import textwrap
import sys

from concourse.lib.constants import REGISTRY_IMAGE
from concourse.lib.models.pipeline import (  # noqa: WPS235
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
from concourse.lib.resource_types import s3_sync
from concourse.lib.resources import git_repo

ovs_release = git_repo(
    Identifier("odl-video-service"),
    uri="https://github.com/mitodl/odl-video-service",
    branch="master",
    check_every="60m",
)

docker_registry_image = Resource(
    name=Identifier("ovs-image"),
    type="registry-image",
    icon="docker",
    source={
        "password": "((dockerhub.password))",
        "username": "((dockerhub.username))",
        "repository": "mitodl/ovs-app",
        "tag": "latest",
    },
)

dcind_resource = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="mitodl/concourse-dcind", tag="latest"),
)


def build_docker_image_pipeline() -> Pipeline:
    build_docker_image_job = Job(
        name="build-docker-image",
        build_log_retention={"builds": 10},
        plan=[
            GetStep(get=ovs_release.name, trigger=True),
            TaskStep(
                task=Identifier("build-ovs-app"),
                privileged=True,
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=dcind_resource,
                    inputs=[Input(name=ovs_release.name)],
                    outputs=[Output(name=ovs_release.name)],
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
                            textwrap.dedent(  # noqa: WPS462
                                """mount -t cgroup -o none,name=systemd cgroup /sys/fs/cgroup/systemd
                                cd odl-video-service
                                chmod 777 .
                                env > .env
                                docker-compose build
                                docker-compose run -u root watch yarn install --frozen-lockfile --ignore-engines --prefer-offline
                                docker-compose run -u root watch node node_modules/webpack/bin/webpack.js --config webpack.config.prod.js --bail
                                IMG_ID="$(docker images | grep "odl-video-service_python" | tr -s ' ' | cut -d ' ' -f 3)"
                                docker create --name ovs-app "${IMG_ID}"
                                sleep 10
                                docker cp ./static ovs-app:/src
                                docker cp ./webpack-stats.json ovs-app:/src
                                docker commit ovs-app ovs-app:latest
                                FINAL_IMG_ID="$(docker images | grep "ovs-app" |  tr -s ' ' | cut -d ' ' -f 3)"
                                docker save "${FINAL_IMG_ID}" -o ovs-app.tar"""
                            ),  # noqa: WPS355
                        ],
                    ),
                ),
            ),
            PutStep(
                put=docker_registry_image.name,
                params={
                    "image": f"{ovs_release.name}/ovs-app.tar",
                    "additional_tags": f"./{ovs_release.name}/.git/describe_ref",
                },
            ),
        ],
    )
    return Pipeline(
        resource_types=[s3_sync()],
        resources=[
            ovs_release,
            docker_registry_image,
        ],
        jobs=[build_docker_image_job],
    )


if __name__ == "__main__":
    with open("definition.json", "w") as definition:
        definition.write(docker_pipeline().json(indent=2))
    sys.stdout.write(docker_pipeline().json(indent=2))
    sys.stdout.writelines(
        ("\n", "fly -t pr-inf sp -p docker-ovs-image -c definition.json")
    )