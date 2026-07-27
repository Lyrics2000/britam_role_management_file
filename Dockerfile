############################################################
# ADR: nginx:alpine over python http.server or node serve —
# ~10MB image, production-grade static file serving, correct
# MIME types, gzip, and caching headers out of the box.
############################################################
FROM nginx:1.27-alpine

# Custom nginx config (gzip, caching, health endpoint)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy site content. Copies ALL html/css/js/assets in the
# build context so relative links and images keep working.
COPY . /usr/share/nginx/html/

############################################################
# Main file is Britam_Role_Library.html — serve it at "/"
# by making it the index. (cp, not mv, so the original URL
# /Britam_Role_Library.html also still works.)
############################################################
RUN cp /usr/share/nginx/html/Britam_Role_Library.html \
       /usr/share/nginx/html/index.html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://localhost/healthz || exit 1
