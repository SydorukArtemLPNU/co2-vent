"""Ввід-вивід хмарного прогону: сертифікати з SSM Parameter Store, результати в S3.

python cloud/cloud_io.py fetch-certs            -> certs/*.pem (права 600)
python cloud/cloud_io.py upload <dir> <prefix>  -> s3://$RESULTS_BUCKET/<prefix>/...
"""
import os
import sys
from pathlib import Path

import boto3

PARAMS = {  # SSM-параметр (SecureString) -> файл
    "/co2vent/certs/ca": "certs/AmazonRootCA1.pem",
    "/co2vent/certs/server-cert": "certs/server.pem.crt",
    "/co2vent/certs/server-key": "certs/server.private.pem.key",
    "/co2vent/certs/node-cert": "certs/node.pem.crt",
    "/co2vent/certs/node-key": "certs/node.private.pem.key",
}


def fetch_certs():
    ssm = boto3.client("ssm")
    got = ssm.get_parameters(Names=list(PARAMS), WithDecryption=True)
    if got["InvalidParameters"]:
        sys.exit(f"missing SSM parameters: {got['InvalidParameters']}")
    Path("certs").mkdir(mode=0o700, exist_ok=True)
    for p in got["Parameters"]:
        path = Path(PARAMS[p["Name"]])
        path.write_text(p["Value"])
        path.chmod(0o600)
    print(f"certs: {len(got['Parameters'])} files from SSM")


def upload(local, prefix):
    bucket = os.environ["RESULTS_BUCKET"]
    s3 = boto3.client("s3")
    count = 0
    for path in Path(local).rglob("*"):
        if path.is_file():
            ctype = "text/plain; charset=utf-8" if path.suffix in (".log", ".md", ".jsonl", ".json") else None
            extra = {"ContentType": ctype} if ctype else {}
            s3.upload_file(str(path), bucket, f"{prefix}/{path.relative_to(local)}", ExtraArgs=extra)
            count += 1
    print(f"uploaded {count} files")


if __name__ == "__main__":
    if sys.argv[1:2] == ["fetch-certs"]:
        fetch_certs()
    elif sys.argv[1:2] == ["upload"] and len(sys.argv) == 4:
        upload(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
