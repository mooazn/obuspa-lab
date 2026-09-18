# Builds OB-USP-AGENT plus the data-model proxy plug-in.
#
# Derived from obuspa's own Dockerfile, with two changes:
#   - obuspa is pinned to a release tag rather than tracking master, so the
#     vendor API the plug-in compiles against cannot move underneath us
#   - our plug-in is compiled against the obuspa headers and shipped alongside

ARG OBUSPA_VERSION=v11.0.0-master
# Pinned: libwebsockets master currently fails to configure (a webtransport
# example declares a target with no sources).
ARG LWS_VERSION=v4.5.8

FROM debian:stable-slim AS build-stage
ARG OBUSPA_VERSION
ARG LWS_VERSION

RUN apt-get update && apt-get -y install --no-install-recommends \
        build-essential autoconf automake libtool pkg-config cmake git ca-certificates \
        libssl-dev libcurl4-openssl-dev libsqlite3-dev libz-dev libmosquitto-dev libcjson-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/local/src

# obuspa requires libwebsockets, which Debian does not package at a usable version
RUN git clone --depth 1 --branch ${LWS_VERSION} https://github.com/warmcat/libwebsockets.git libwebsockets \
    && cd libwebsockets \
    && cmake -DLWS_WITH_SSL=ON \
             -DLWS_WITHOUT_TESTAPPS=ON \
             -DLWS_WITHOUT_TEST_SERVER=ON \
             -DLWS_WITHOUT_TEST_CLIENT=ON \
             -DLWS_WITH_MINIMAL_EXAMPLES=OFF . \
    && make -j"$(nproc)" && make install
RUN ldconfig

RUN git clone --depth 1 --branch ${OBUSPA_VERSION} https://github.com/BroadbandForum/obuspa.git obuspa \
    && cd obuspa && autoreconf --force --install && ./configure && make -j"$(nproc)" && make install

# Build the plug-in against the obuspa headers we just checked out
COPY plugin/ /usr/local/src/vdev-plugin/
RUN make -C /usr/local/src/vdev-plugin OBUSPA_SRC=/usr/local/src/obuspa


FROM debian:stable-slim
RUN apt-get update && apt-get -y install --no-install-recommends \
        libssl3 libsqlite3-0 libcurl4 libmosquitto1 libcjson1 jq util-linux libfaketime \
    && rm -rf /var/lib/apt/lists/* \
    # The lab clock: obuspa runs under libfaketime so that the device can move
    # the firmware's idea of the time. One architecture-independent path for
    # the entrypoint to preload.
    && ln -s "$(dpkg -L libfaketime | grep 'libfaketime.so.1$')" /usr/local/lib/libfaketime.so.1

COPY --from=build-stage /usr/local/lib/libwebsockets.so* /usr/local/lib/
COPY --from=build-stage /usr/local/bin/obuspa /usr/local/bin/obuspa
COPY --from=build-stage /usr/local/src/vdev-plugin/vdev_plugin.so /usr/local/lib/vdev_plugin.so
RUN ldconfig

COPY agent/factory_reset.txt /etc/obuspa/factory_reset.txt
COPY agent/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY agent/faultd.sh /usr/local/bin/faultd.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/faultd.sh

# Socket to the device service, and the agent database
VOLUME ["/run/vdev"]
ENV VDEV_SOCK=/run/vdev/vdev.sock

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
