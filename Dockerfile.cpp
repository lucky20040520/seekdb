FROM python:3.12.8-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends g++ cmake nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY cpp /src
RUN mkdir -p /build && g++ -std=c++17 -O3 -DNDEBUG -ffp-contract=off -Wall -Wextra -Wpedantic \
    /src/main.cpp /src/storage.cpp /src/sql.cpp /src/query.cpp -o /build/minivector

# Keep the same Debian base as the measured Python image. Runtime entry is C++.
FROM python:3.12.8-slim-bookworm
RUN mkdir -p /data
COPY --from=build /build/minivector /usr/local/bin/minivector
COPY cpp/LICENSE.unicode.txt /usr/share/licenses/minivector/UNICODE.txt
ENTRYPOINT ["/usr/local/bin/minivector", "--data-dir", "/data"]
