# ============================================================================
# FRONTEND - React + Tailwind chat UI, built and served as static files.
#
# Multi-stage: Node builds the production bundle, then a tiny nginx image
# serves it - the final image never carries node_modules or the Node
# toolchain, just the ~150KB built bundle plus nginx.
# ============================================================================

# ---- build stage ----
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

# ---- serve stage ----
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
# 3000, not nginx's usual 80: this container runs with network_mode: host
# (see ../chatbot_pipeline/docker-compose.yml), so this is the real port on
# the VM, not just a mapped one - keeping it off 80 avoids clashing with
# anything else already using that port on the host.
EXPOSE 3000
