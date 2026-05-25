import boto3
import os
from urllib.parse import urlparse

s3 = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("AWS_S3_REGION"),
)

def generate_presigned_url_from_s3_uri(s3_uri: str, expiry: int = 3600):
    try:
        # Parse s3:// URI
        parsed = urlparse(s3_uri)

        bucket = parsed.netloc
        key = parsed.path.lstrip("/")

        if not bucket or not key:
            raise ValueError("Invalid S3 URI format")

        # Generate pre-signed URL
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key
            },
            ExpiresIn=expiry
        )

        return url

    except Exception as e:
        raise Exception(f"Error generating signed URL: {str(e)}")
        
        
    s3_uri = "s3://ai-voice-agent-call-recording-data-373942188845-ap-south-1-an/montra_nps_outbound/efdc53f5-b187-4d85-9d27-c31201c5d65e_20260507_182844.wav"

signed_url = generate_presigned_url_from_s3_uri(s3_uri)

print(signed_url)