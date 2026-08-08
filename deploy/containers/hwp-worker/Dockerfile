FROM rust:1.93.1-bookworm@sha256:7c4ae649a84014c467d79319bbf17ce2632ae8b8be123ac2fb2ea5be46823f31 AS rhwp-builder

ARG RHWP_VERSION=0.8.2
ARG RHWP_COMMIT=9b16aa9e23f476e2b335d7c029fc9f24a199d63c
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none https://github.com/edwardkim/rhwp.git /src/rhwp \
    && git -C /src/rhwp checkout --detach "${RHWP_COMMIT}" \
    && test "$(git -C /src/rhwp rev-parse HEAD)" = "${RHWP_COMMIT}" \
    && test "$(sed -n 's/^version = "\([^"]*\)"/\1/p' /src/rhwp/Cargo.toml | head -n1)" = "${RHWP_VERSION}" \
    && cargo build --manifest-path /src/rhwp/Cargo.toml --locked --release --bin rhwp

FROM python:3.12.10-slim@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ARG RHWP_VERSION=0.8.2
ARG RHWP_COMMIT=9b16aa9e23f476e2b335d7c029fc9f24a199d63c
ARG RUST_VERSION=1.93.1
ARG QPDF_DEBIAN_VERSION=11.3.0-1+deb12u1
ARG NOTO_CJK_DEBIAN_VERSION=1:20220127+repack1-1
ARG RUNTIME_BASE_IMAGE_DIGEST=sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=UTC \
    HWP_JOB_TIMEOUT_SECONDS=180 \
    HWP_MAX_INPUT_BYTES=134217728 \
    HWP_MAX_OUTPUT_BYTES=314572800 \
    HWP_MAX_REPORT_BYTES=65536 \
    HWP_MAX_PAGES=2000 \
    HWP_MAX_LOG_BYTES=1048576 \
    HWP_RLIMIT_CPU_SECONDS=180 \
    HWP_RLIMIT_AS_BYTES=536870912 \
    HWP_RLIMIT_FSIZE_BYTES=314572800 \
    HWP_RLIMIT_NOFILE=64 \
    HWP_RLIMIT_NPROC=32

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       "qpdf=${QPDF_DEBIAN_VERSION}" \
       "fonts-noto-cjk=${NOTO_CJK_DEBIAN_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/cache/fontconfig/* /etc/fonts/conf.d/* \
    && groupadd --gid 65531 wisdome-hwp-validator \
    && useradd --uid 65531 --gid 65531 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin wisdome-hwp-validator \
    && groupadd --gid 65533 wisdome-hwp-child \
    && useradd --uid 65533 --gid 65533 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin wisdome-hwp-child \
    && install -d -o root -g root -m 0700 /run/wisdome-hwp /work /input \
    && install -d -o root -g root -m 0555 /opt/wisdome/manifest /etc/wisdome \
       /usr/share/licenses/rhwp /usr/share/wisdome/rhwp

COPY --from=rhwp-builder /src/rhwp/target/release/rhwp /usr/local/bin/rhwp
COPY deploy/containers/hwp-worker/wisdome-hwp-sandbox /usr/local/bin/wisdome-hwp-sandbox
COPY deploy/containers/hwp-worker/build_manifest.py /usr/local/libexec/wisdome-build-hwp-manifest
COPY deploy/containers/hwp-worker/fonts.conf /etc/wisdome/fonts.conf
COPY compose.yaml /etc/wisdome/compose.yaml
COPY --from=rhwp-builder /src/rhwp/LICENSE /usr/share/licenses/rhwp/LICENSE
COPY --from=rhwp-builder /src/rhwp/Cargo.lock /usr/share/wisdome/rhwp/Cargo.lock

RUN chmod 0555 /usr/local/bin/rhwp /usr/local/bin/wisdome-hwp-sandbox \
    && chmod 0444 /usr/local/libexec/wisdome-build-hwp-manifest \
    && chmod 0444 /etc/wisdome/fonts.conf /etc/wisdome/compose.yaml \
       /usr/share/licenses/rhwp/LICENSE /usr/share/wisdome/rhwp/Cargo.lock \
    && python -c "import json; from pathlib import Path; Path('/etc/wisdome/hwp-sandbox-policy.json').write_text(json.dumps({'schema_version':'v1','network_allowed':False,'read_only_rootfs':True,'read_only_input':True,'private_tmpfs':True,'non_root':True,'resource_limits_enforced':True},sort_keys=True,separators=(',',':'))+'\n',encoding='utf-8')" \
    && chmod 0444 /etc/wisdome/hwp-sandbox-policy.json \
    && python -c "import json,subprocess; from pathlib import Path; qpdf=subprocess.check_output(['/usr/bin/qpdf','--version'],text=True).splitlines()[0]; package=subprocess.check_output(['dpkg-query','-W','qpdf'],text=True).strip(); Path('/etc/wisdome/hwp-build-material.json').write_text(json.dumps({'schema_version':'v1','runtime_base_image_digest':'${RUNTIME_BASE_IMAGE_DIGEST}','rhwp_commit':'${RHWP_COMMIT}','rust_version':'${RUST_VERSION}','qpdf_observed_version':qpdf,'qpdf_source_package':'qpdf','qpdf_debian_package':package,'build_network_hermetic':False,'runtime_self_report_is_policy_evidence':False},sort_keys=True,separators=(',',':'))+'\n',encoding='utf-8')" \
    && chmod 0444 /etc/wisdome/hwp-build-material.json \
    && python /usr/local/libexec/wisdome-build-hwp-manifest \
       --output /opt/wisdome/manifest/converter-manifest.json \
       --rhwp-version "${RHWP_VERSION}" \
       --rhwp-commit "${RHWP_COMMIT}" \
       --rust-version "${RUST_VERSION}" \
       --entry converter=/usr/local/bin/rhwp \
       --entry qpdf=/usr/bin/qpdf \
       --entry wrapper=/usr/local/bin/wisdome-hwp-sandbox \
       --entry config=/etc/wisdome/hwp-sandbox-policy.json \
       --entry config=/etc/wisdome/compose.yaml \
       --entry build-metadata=/etc/wisdome/hwp-build-material.json \
       --entry license=/usr/share/licenses/rhwp/LICENSE \
       --entry lockfile=/usr/share/wisdome/rhwp/Cargo.lock \
       --entry runtime=/usr/local/bin/python3.12 \
       --directory font=/usr/share/fonts/opentype/noto \
       --entry fontconfig=/etc/wisdome/fonts.conf \
       --directory runtime=/usr/local/lib/python3.12 \
       --loaded-libraries-for /usr/local/bin/rhwp \
       --loaded-libraries-for /usr/bin/qpdf \
       --loaded-libraries-for /usr/local/bin/python3.12 \
    && chmod 0444 /opt/wisdome/manifest/converter-manifest.json

USER 0:0
ENTRYPOINT ["/usr/local/bin/wisdome-hwp-sandbox"]
CMD ["serve", "--socket", "/run/wisdome-hwp/converter.sock", "--input-root", "/input", "--work-root", "/work", "--manifest", "/opt/wisdome/manifest/converter-manifest.json"]
