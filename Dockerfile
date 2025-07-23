# This Dockerfile sets up an environment for running an MLflow server with Python 3.11 and necessary dependencies.
FROM python:3.11-slim

# Install MLflow and dependencies
RUN pip install mlflow boto3 psycopg2-binary

# Create a non-root user (recommended for security)
RUN useradd -ms /bin/bash mlflowuser
USER mlflowuser
WORKDIR /home/mlflowuser
