# What this file does: builds the portal (Node) and serves the static files with nginx,
# which also forwards /v1 to the API container. Two stages: the final image has no Node.
FROM node:22.12-alpine AS build
WORKDIR /portal
COPY package.json package-lock.json .npmrc ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27.3-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /portal/dist /usr/share/nginx/html
