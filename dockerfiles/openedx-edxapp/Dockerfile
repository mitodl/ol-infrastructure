# Heavily inspired by Tutor from overhang.io
FROM --platform=linux/amd64 python:3.8.12-buster as base

ENV DEBIAN_FRONTEND=noninteractive
COPY ./apt-pkg-list /root
COPY ./pip-pkg-list /root
RUN apt update \
    && apt install -y --no-install-recommends $(cat /root/apt-pkg-list) \
    && apt autoremove -y \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*
