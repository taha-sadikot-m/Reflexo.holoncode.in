# reflexo-policy-check: the Cedar rule checker (BETA_04 §7.6), a Linux binary with the cvc5 solver beside it.
# Local use goes through `docker run` (REFLEXO_POLICY_CHECK_CMD); production images copy /usr/local/bin from here.
FROM rust:1.90.0-bookworm AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim AS solver
ARG CVC5_VERSION=1.3.1
ARG CVC5_SHA256=1a1cda20d2df4938fa4944a69f33ddc9172e319ece0eed0aa09c4d7abede3ed1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/cvc5.zip \
      "https://github.com/cvc5/cvc5/releases/download/cvc5-${CVC5_VERSION}/cvc5-Linux-x86_64-static.zip" \
 && echo "${CVC5_SHA256}  /tmp/cvc5.zip" | sha256sum -c - \
 && unzip -q /tmp/cvc5.zip -d /tmp/cvc5 \
 && install -m 0755 "$(find /tmp/cvc5 -type f -name cvc5 -path '*/bin/*' | head -1)" /usr/local/bin/cvc5

FROM debian:bookworm-slim
COPY --from=solver /usr/local/bin/cvc5 /usr/local/bin/cvc5
COPY --from=build /src/target/release/reflexo-policy-check /usr/local/bin/reflexo-policy-check
ENV CVC5=/usr/local/bin/cvc5
USER 65534:65534
ENTRYPOINT ["/usr/local/bin/reflexo-policy-check"]
