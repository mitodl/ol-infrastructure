This is the Dockerfile for
https://hub.docker.com/r/mitodl/ocw-course-publisher

The docker image is used in the Concourse pipeline for building the OCW
site. See [pipelines/ocw/docker-image-pipeline.yml](/pipelines/ocw/docker-image-pipeline.yml)

This should be built and tagged as mitodl/ocw-course-publisher:latest and
pushed to Docker Hub:

```
	$ docker build -t mitodl/ocw-course-publisher:latest .
	$ docker push mitodl/ocw-course-publisher:latest`
```
