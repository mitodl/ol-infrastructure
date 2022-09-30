import textwrap

from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    PutStep,
    RegistryImage,
    Resource,
    TaskConfig,
    TaskStep,
)
from concourse.lib.resource_types import rclone
from concourse.lib.resources import git_repo

ovs_release = git_repo(
    Identifier("odl-video-service"),
    uri="https://github.com/mitodl/odl-video-service",
    branch="master",
    check_every="60m",
)

rclone_resource = Resource(
    name=Identifier(f"rclone-static-assets"),
    icon="cloud-outline",
    type="rclone",
    source={
        "config": textwrap.dedent(
            """\
        [s3-remote]
        type = s3
        provider = AWS
        env_auth = true
        region = us-east-1
        """
        )
    },
)

dcind_resource = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="mitodl/concourse-dcind", tag="latest"),
)


def static_assets_pipeline() -> Pipeline:
    build_static_assets_job = Job(
        name="build-static-assets",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=ovs_release.name, trigger=True),
            TaskStep(
                task=Identifier("build-ovs-staticfiles"),
                privileged=True,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=dcind_resource,
                    inputs=[Input(name=ovs_release.name)],
                    outputs=[Output(name="staticfiles"), Output(name="static")],
                    # https://github.com/mitodl/odl-video-service/blob/master/.github/workflows/ci.yml#L63-L97
                    params={
                        "DEBUG": "False",
                        "NODE_ENV": "production",
                        "DATABASE_URL": "postgres://postgres:postgres@localhost:5432/postgres",  # pragma: allowlist secret
                        "ODL_VIDEO_SECURE_SSL_REDIRECT": "False",
                        "ODL_VIDEO_DB_DISABLE_SSL": "True",
                        "CELERY_TASK_ALWAYS_EAGER": "True",
                        "REDIS_URL": "redis://localhost:6379/4",
                        "XDG_CACHE_HOME": "/src/.cache",
                        "SECRET_KEY": "actions_secret",  # pragma: allowlist secret
                        "AWS_ACCESS_KEY_ID": "fakeawskey",  # pragma: allowlist secret
                        "AWS_SECRET_ACCESS_KEY": "fakeawssecret",  # pragma: allowlist secret
                        "AWS_REGION": "us-east-1",
                        "CLOUDFRONT_KEY_ID": "cfkeyid",
                        "DROPBOX_KEY": "foo_dropbox_key",  # pragma: allowlist secret
                        "ET_PIPELINE_ID": "foo_et_pipeline_id",
                        "FIELD_ENCRYPTION_KEY": "jtma0CL1QMRLaJgjXNlJh3mtPNcgok0G5ajRCMZ_XNI=",  # pragma: allowlist secret
                        "GA_DIMENSION_CAMERA": "fake",
                        "GA_KEYFILE_JSON": "fake",
                        "GA_VIEW_ID": "fake",
                        "GA_TRACKING_ID": "fake",
                        "LECTURE_CAPTURE_USER": "admin",
                        "MAILGUN_URL": "http://fake_mailgun_url.com",
                        "MAILGUN_KEY": "foookey",  # pragma: allowlist secret
                        "ODL_VIDEO_BASE_URL": "http://video.example.com",
                        "VIDEO_S3_BUCKET": "video-s3",
                        "VIDEO_S3_TRANSCODE_BUCKET": "video-s3-transcodes",
                        "VIDEO_S3_THUMBNAIL_BUCKET": "video-s3-thumbs",
                        "VIDEO_S3_SUBTITLE_BUCKET": "video-s3-subtitles",
                        "VIDEO_S3_WATCH_BUCKET": "video-s3-watch",
                        "VIDEO_CLOUDFRONT_DIST": "video-cf",
                        "YT_ACCESS_TOKEN": "fake",
                        "YT_REFRESH_TOKEN": "fake",
                        "YT_CLIENT_ID": "fake",
                        "YT_CLIENT_SECRET": "false",  # pragma: allowlist secret
                        "YT_PROJECT_ID": "fake",
                    },
                    run=Command(
                        path="/bin/entrypoint.sh",
                        args=[
                            "bash",
                            "-ceux",
                            textwrap.dedent(
                                """mount -t cgroup -o none,name=systemd cgroup /sys/fs/cgroup/systemd
                                 cd odl-video-service
                                 chmod 777 .
                                 env > .env
                                 docker-compose build
                                 whoami
                                 docker-compose run -u root watch yarn install --frozen-lockfile --ignore-engines --prefer-offline
                                 docker-compose run -u root watch node node_modules/webpack/bin/webpack.js --config webpack.config.prod.js --bail
                                 docker-compose run web python manage.py collectstatic --no-input
                                 WEBPACK_SUFFIX="$(cat .git/describe_ref)"
                                 cp webpack-stats.json static/webpack-stats.json.${WEBPACK_SUFFIX}
                                 cp -r static ../
                                 cp -r staticfiles ../"""
                            ),
                        ],
                    ),
                ),
            ),
            PutStep(
                put=rclone_resource.name,
                get_params={"skip_implicit_get": True},
                params={
                    "source": "static",
                    "destination": [
                        {
                            "command": "sync",
                            "dir": "s3-remote:ovs-static-assets-ci/mike-testing/static/",  # TODO Revisit
                        }
                    ],
                },
            ),
            PutStep(
                put=rclone_resource.name,
                get_params={"skip_implicit_get": True},
                params={
                    "source": "staticfiles",
                    "destination": [
                        {
                            "command": "sync",
                            "dir": "s3-remote:ovs-static-assets-ci/mike-testing/staticfiles/",  # TODO Revisit
                        }
                    ],
                },
            ),
        ],
    )
    return Pipeline(
        resource_types=[rclone()],
        resources=[ovs_release, rclone_resource],
        jobs=[build_static_assets_job],
    )


if __name__ == "__main__":
    import sys

    with open("definition.json", "wt") as definition:
        definition.write(static_assets_pipeline().json(indent=2))
    sys.stdout.write(static_assets_pipeline().json(indent=2))
    print()
    print("fly -t pr-inf sp -p static-assets-ovs -c definition.json")
