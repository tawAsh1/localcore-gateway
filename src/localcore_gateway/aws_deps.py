"""Gate for the optional ``aws`` extra (boto3/botocore).

The real-AWS passthrough features (``type: aws-gateway`` targets and
``lambda.backend: aws``) need boto3, which is deliberately NOT a base
dependency -- the core gateway stays AWS-SDK-free. Import through here so a
missing extra surfaces as a config-time error with the fix in the message.
"""

from __future__ import annotations

from typing import Any


def require_boto3() -> Any:
    """Import and return boto3, or raise with the install hint."""
    try:
        import boto3
    except ImportError as exc:
        raise ValueError("this target/backend requires the aws extra: pip install 'localcore-gateway[aws]'") from exc
    return boto3
