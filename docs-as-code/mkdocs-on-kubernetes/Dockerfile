# Build the MkDocs site, then serve it as static files from an unprivileged nginx.
# Two stages so the runtime image has no Python, no build tools, no source.
#
#   docker build -t docs:local .
#   docker run --rm -p 8080:8080 docs:local      -> http://127.0.0.1:8080

FROM squidfunk/mkdocs-material:9 AS build
WORKDIR /docs
COPY requirements.txt .
# The Material image ships mkdocs-material; requirements.txt adds the plugins the site uses.
RUN pip install --no-cache-dir -q -r requirements.txt
COPY mkdocs.yml .
COPY docs/ docs/
RUN mkdocs build --strict --site-dir /site

FROM nginxinc/nginx-unprivileged:1.27-alpine
# nginx-unprivileged listens on 8080 as uid 101; nothing here needs root.
COPY --from=build /site /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
