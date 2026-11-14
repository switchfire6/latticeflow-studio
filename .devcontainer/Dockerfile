# The devcontainers Python image ships a Yarn apt source whose signing key no longer
# verifies, so every `apt-get update` (run by the node and sshd features) fails and
# Codespaces falls back to a recovery container. The studio uses npm, so drop it.
FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm
RUN rm -f /etc/apt/sources.list.d/yarn.list
