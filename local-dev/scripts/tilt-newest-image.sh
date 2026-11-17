#!/usr/bin/env bash
# Prints the image an apply command should deploy, given the one Tilt handed it
# in TILT_IMAGE_0. Prints nothing when Tilt built none.
#
# When several resources take one docker_build image, Tilt can hand an apply
# that did not build the image an older tag than the newest build. Each
# resource's last build of the image is a DockerImage object, and Tilt copies
# whichever of those it reconciled last into the ImageMap that TILT_IMAGE_0 is
# read from (maybeUpdateImageMapStatus in Tilt's dockerimage reconciler), newer
# or not. So the tag printed is the newest of those builds, except where the
# objects cannot show that TILT_IMAGE_0 is older. They hold each resource's
# latest attempt, not its builds:
# - A finished build reaches its object from a separate reconcile, which the
#   apply does not wait for. While an object is still marked as building,
#   TILT_IMAGE_0 is that build and stands. An older object can name the same
#   tag, because tags follow the image's content.
# - A failed build replaces the object's tag with an error. A tag that no
#   object names may be newer than every tag they do name, and it stands.
set -euo pipefail
image="${TILT_IMAGE_0:-}"
[ -n "$image" ] || exit 0
repository="${image%:*}"
handed="${image##*:}"
tag="$(tilt get dockerimage -o json | jq -r --arg name "${repository##*/}" --arg handed "$handed" '
    [.items[] | select(.metadata.name | endswith(":" + $name)) | .status // {}]
    | map(select(.completed and .ref)) as $built
    | if any(.building) or ($built | any(.ref | endswith(":" + $handed)) | not)
      then empty
      else $built | max_by(.completed.finishedAt) | .ref | sub(".*:"; "")
      end')"
[ "${tag:-$handed}" = "$handed" ] || echo "Tilt handed this apply $handed; deploying the newer build $tag" >&2
echo "$repository:${tag:-$handed}"
