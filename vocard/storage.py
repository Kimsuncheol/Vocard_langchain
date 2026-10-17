import hashlib
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class PrivateS3:
    """No public URLs or ACLs. Every operation fails closed on an unsafe bucket."""

    def __init__(self, bucket: str, client=None):
        self.bucket = bucket
        self.client = client or boto3.client(
            "s3",
            config=Config(
                retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=10, read_timeout=60
            ),
        )

    def assert_private(self):
        block = self.client.get_public_access_block(Bucket=self.bucket)[
            "PublicAccessBlockConfiguration"
        ]
        if not all(
            block.get(k) is True
            for k in (
                "BlockPublicAcls",
                "IgnorePublicAcls",
                "BlockPublicPolicy",
                "RestrictPublicBuckets",
            )
        ):
            raise ValueError("S3 bucket must enable all four Block Public Access settings")
        try:
            policy = self.client.get_bucket_policy_status(Bucket=self.bucket)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchBucketPolicy":
                raise
        else:
            if policy["PolicyStatus"]["IsPublic"]:
                raise ValueError("S3 bucket policy is public")

    def put(self, key: str, body: BinaryIO, sha256: str, content_type: str):
        self.assert_private()
        body.seek(0)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                ServerSideEncryption="AES256",
                Metadata={"sha256": sha256},
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("PreconditionFailed", "412"):
                raise
            existing = self.client.head_object(Bucket=self.bucket, Key=key)
            if existing.get("Metadata", {}).get("sha256") != sha256:
                raise ValueError("Existing immutable object has a conflicting checksum") from exc

    def get(self, key: str, expected_sha: str | None = None) -> bytes:
        self.assert_private()
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        stream = response["Body"]
        try:
            data = stream.read()
        finally:
            stream.close()
        actual = hashlib.sha256(data).hexdigest()
        if expected_sha is not None and actual != expected_sha:
            raise ValueError("Object checksum validation failed")
        if response.get("Metadata", {}).get("sha256") != actual:
            raise ValueError("Object metadata checksum validation failed")
        return data
