FROM ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852

ARG BUILD_JOBS=4

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential cmake git curl ca-certificates python3 time bc file wget unzip dos2unix patch \
    && rm -rf /var/lib/apt/lists/*

RUN git init /suite \
    && git -C /suite remote add origin https://github.com/daanx/mimalloc-bench.git \
    && git -C /suite fetch --depth 1 origin 198cec2701676965970e8f0012897b8da304b3f2 \
    && git -C /suite checkout --detach FETCH_HEAD \
    && test "$(git -C /suite rev-parse HEAD)" = "198cec2701676965970e8f0012897b8da304b3f2" \
    && git -C /suite rev-parse HEAD > /suite/suite-commit.txt

WORKDIR /suite
RUN mkdir -p extern/pmalloc out/bench \
    && git init extern/mi3 \
    && git -C extern/mi3 remote add origin https://github.com/microsoft/mimalloc.git \
    && git -C extern/mi3 fetch --depth 1 origin b18ab4eef383f655e760881b02eff8af02800879 \
    && git -C extern/mi3 checkout --detach FETCH_HEAD \
    && test "$(git -C extern/mi3 rev-parse HEAD)" = "b18ab4eef383f655e760881b02eff8af02800879" \
    && git -C extern/mi3 rev-parse HEAD > /suite/mimalloc-commit.txt \
    && cmake -S extern/mi3 -B extern/mi3/out/release -DCMAKE_BUILD_TYPE=Release \
       -DMI_BUILD_TESTS=OFF -DMI_BUILD_OBJECT=OFF -DMI_BUILD_STATIC=OFF -DMI_BUILD_SHARED=ON \
    && cmake --build extern/mi3/out/release --parallel "${BUILD_JOBS}" \
    && ./build-bench-env.sh bench -j="${BUILD_JOBS}"

COPY allocators/ /opt/allocators/
COPY preload.cpp smoke.cpp /opt/
COPY LICENSE-pmalloc /opt/licenses/LICENSE-pmalloc
RUN for variant in old new; do \
      g++ -std=c++17 -O3 -DNDEBUG -fPIC -ftls-model=initial-exec -fvisibility=hidden \
        -fno-builtin-malloc -fno-builtin-calloc -fno-builtin-realloc -fno-builtin-free \
        -fno-builtin-strdup -fno-builtin-strndup -shared -pthread \
        -I"/opt/allocators/${variant}" /opt/preload.cpp \
        -o "/suite/extern/pmalloc/libpmalloc_${variant}.so" || exit 1; \
    done \
    && g++ -std=c++17 -O2 -pthread -fno-builtin /opt/smoke.cpp -ldl -o /opt/allocator-smoke \
    && printf 'old /suite/extern/pmalloc/libpmalloc_old.so\nnew /suite/extern/pmalloc/libpmalloc_new.so\n' \
       > /suite/pmalloc-external.txt

COPY run.py report.py PROVENANCE.json /opt/
WORKDIR /suite/out/bench
ENTRYPOINT ["python3", "/opt/run.py"]
