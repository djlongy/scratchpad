# A tiny image family whose versions share layers, so the transfer has something to dedupe.
#   layer 1  base image (shared by every image and version)
#   layer 2  "runtime" (shared by every version of both apps)
#   layer 3  app code, one per app (shared by every version of that app)
#   layer 4  the version file, unique per version
ARG BASE=alpine:3.20
FROM ${BASE}
RUN echo "runtime $(date +%F)" > /runtime.txt && dd if=/dev/urandom of=/runtime.bin bs=1M count=2 status=none
ARG APP
RUN echo "app ${APP}" > /app.txt && dd if=/dev/urandom of=/app.bin bs=1M count=1 status=none
ARG VERSION
RUN echo "${APP} ${VERSION}" > /version.txt
CMD ["cat", "/version.txt"]
