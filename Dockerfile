FROM debian:bookworm-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY Makefile ./
COPY src/ src/
COPY tests/test_map.c tests/
RUN make && make test-map && ./test-map

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends python3 fio util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data /results /opt/cachescope
COPY --from=build /build/cachescope /usr/local/bin/cachescope
COPY cachescope_runner/ /opt/cachescope/cachescope_runner/
COPY scripts/entrypoint.sh /usr/local/bin/cachescope-entrypoint
ENV PYTHONPATH=/opt/cachescope PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /data
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/cachescope-entrypoint"]
CMD ["--help"]

FROM runtime AS test
USER root
COPY tests/ /opt/cachescope/tests/
WORKDIR /opt/cachescope
CMD ["python3", "tests/run.py"]
ENTRYPOINT []

FROM runtime AS final
