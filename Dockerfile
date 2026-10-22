FROM public.ecr.aws/docker/library/node:22-bookworm-slim AS wecom
RUN npm install --prefix /opt/wecom @wecom/cli-linux-x64@1.3.4

FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY --from=wecom /opt/wecom/node_modules/@wecom/cli-linux-x64/bin/wecom-cli /usr/local/bin/wecom-cli
COPY index.html iteration-plan.html data-refresh.js runtime_server.py ./
COPY scripts/refresh_dashboard_from_wecom.py ./scripts/
COPY config/wecom_yiduoyun_config.json ./config/
RUN mkdir -p /root/.config && ln -s /data/wecom /root/.config/wecom
EXPOSE 8080
CMD ["python3", "-u", "runtime_server.py"]
