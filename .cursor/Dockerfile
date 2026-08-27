# Cloud Agent base image for HivePilot.
#
# Self-contained and reproducible from source: the stock base image ships
# Python and Node but NOT python3-venv, so `python3 -m venv .venv` in the
# install step fails with "ensurepip is not available" unless the package is
# present. Pinning it here removes the reliance on a saved snapshot (HP-14).
#
# Ubuntu 24.04 (noble) provides Python 3.12 natively — matching CI — and ships
# a default non-root `ubuntu` user (uid 1000). Node 22 is added from NodeSource
# to match the pinned frontend toolchain (web/.nvmrc). The repository itself is
# checked out separately by Cursor, so nothing is COPYed in here.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        build-essential \
        python3 \
        python3-venv \
        python3-pip \
        python3-dev \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

USER ubuntu
