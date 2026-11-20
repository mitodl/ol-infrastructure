# shellcheck shell=sh
# Put a hotfix's tree in the main checkout before bump-version runs.
#
# Runs in the release cut job between the gets and bump-version. A normal
# release leaves the checkout alone. For a hotfix, the release resource cuts
# production plus the requested commits, then carries bump-version's changes
# from the main checkout onto that tree. Bumping main is wrong for any bump
# that reads the tree: ol-analytics-api's collects every scriv fragment under
# changelog.d/ on main, so a hotfix's CHANGELOG.md entry listed unreleased work
# and its fragments stayed on main to be released again. So this detaches the
# checkout at the commit production was built from and cherry-picks the
# requested commits onto it, building the tree the resource will cut, and the
# bump sees what is released and nothing else. Its changes then apply to the
# resource's cut unchanged, because the trees are the same.
#
# The resource decides what a hotfix contains and refuses the ones it must
# (a conflict, a commit already in production, a release in flight), naming
# the reason. So this never fails the build on a hotfix it cannot reproduce:
# it puts the checkout back on main and leaves the decision to the resource,
# and a bump on main is what ran before this existed.
#
# The base is found the way the release resource finds it: the
# `Release <since>` commit whose first parent is the <since> tag's commit,
# on the ancestry path to main, else the tag itself. The commits are picked
# in the order the resource's `hotfix` file lists them, which is its pick
# order. The repositories are public, so fetches need no credentials.
#
# Environment:
#     REPO:    the main checkout, which bump-version and the put read.
#     RELEASE: the release resource's get, with `hotfix` and `since` files.

hotfix=$(cat "$RELEASE/hotfix")
if [ -z "$hotfix" ]; then
    exit 0
fi
since=$(cat "$RELEASE/since")
cd "$REPO" || exit
main=$(git rev-parse HEAD)

fall_back() {
    echo "prepare-hotfix-source: $1; bumping on main instead." >&2
    git cherry-pick --abort >/dev/null 2>&1 || true
    git checkout --quiet --force --detach "$main"
    exit 0
}

if [ -z "$since" ]; then
    fall_back "there is no release to hotfix"
fi
git fetch --quiet origin "+refs/tags/$since:refs/tags/$since" ||
    fall_back "cannot fetch the $since tag"
tag=$(git rev-list -n1 "$since") || fall_back "cannot read the $since tag"
base=$(
    git log --ancestry-path --format='%H %P%x09%s' "$tag..$main" |
        awk -F '\t' -v tag="$tag" -v subject="Release $since" '
            { split($1, shas, " ") }
            shas[2] == tag && $2 == subject { found = shas[1] }
            END { print found }
        '
) || fall_back "cannot read the history after $since"
if [ -z "$base" ]; then
    base=$tag
fi

git checkout --quiet --detach "$base" || fall_back "cannot check out $base"
for sha in $hotfix; do
    git cat-file -e "$sha^{commit}" 2>/dev/null ||
        git fetch --quiet origin "$sha" ||
        fall_back "cannot fetch $sha"
    mainline=""
    if [ "$(git rev-list --parents -n1 "$sha" | wc -w)" -gt 2 ]; then
        mainline="-m 1"
    fi
    # shellcheck disable=SC2086 # mainline is empty or two words
    git -c user.name=CI -c user.email=odl-devops@mit.edu \
        cherry-pick -x $mainline "$sha" >/dev/null ||
        fall_back "$sha does not apply to $since"
done
echo "prepare-hotfix-source: bumping $since ($base) plus $hotfix" >&2
