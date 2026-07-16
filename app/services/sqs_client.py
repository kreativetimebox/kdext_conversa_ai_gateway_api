"""SQS client — sends job messages to Amazon SQS queues.

This module is used by the gateway to enqueue jobs. The voice-worker
microservice consumes these messages independently.
"""

import json
import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_sqs_client = None


def _get_client():
    """Lazily create a boto3 SQS client."""
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client(
            "sqs",
            region_name=settings.aws_sqs_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    return _sqs_client


def warm_client() -> None:
    """Build the SQS client ahead of first use.

    boto3 client construction (credential/endpoint resolution + first TLS
    handshake to a possibly cross-region queue) is otherwise paid on the first
    enqueue, adding seconds to that request. Called from the app startup hook.
    """
    _get_client()


def send_job(queue_url: str, job_id: int, job_type: str, payload: dict) -> str:
    """Send a job message to the specified SQS queue.

    Args:
        queue_url: Full SQS queue URL.
        job_id: The database request_id for the job.
        job_type: Either "tts" or "stt".
        payload: Job-specific data (text, voice, audio_url, etc.)

    Returns:
        The SQS MessageId.
    """
    message_body = json.dumps({
        "job_id": job_id,
        "job_type": job_type,
        **payload,
    })

    try:
        client = _get_client()
        send_kwargs = {
            "QueueUrl": queue_url,
            "MessageBody": message_body,
        }
        # FIFO queues require MessageGroupId and MessageDeduplicationId
        if ".fifo" in queue_url:
            send_kwargs["MessageGroupId"] = job_type
            send_kwargs["MessageDeduplicationId"] = f"{job_type}-{job_id}"

        response = client.send_message(**send_kwargs)
        message_id = response["MessageId"]
        logger.info("SQS message sent: job_id=%d type=%s MessageId=%s", job_id, job_type, message_id)
        return message_id
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to send SQS message for job_id=%d: %s", job_id, exc)
        raise RuntimeError(f"SQS send failed: {exc}") from exc
