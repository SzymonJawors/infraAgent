resource "random_id" "logs_bucket_suffix" {
  byte_length = 4
}

#checkov:skip=CKV_AWS_109:KMS key policies require wildcard resources; administration is constrained to this account root principal.
#checkov:skip=CKV_AWS_111:KMS key policies require wildcard resources; administration is constrained to this account root principal.
resource "aws_kms_key" "s3_logs" {
  description             = "KMS key for system-sentinel log bucket"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.s3_logs_kms.json
}

resource "aws_kms_alias" "s3_logs" {
  name          = "alias/system-sentinel-s3-logs"
  target_key_id = aws_kms_key.s3_logs.key_id
}

data "aws_iam_policy_document" "s3_logs_kms" {
  #checkov:skip=CKV_AWS_109:KMS key policies require wildcard resources; administration is constrained to this account root principal.
  #checkov:skip=CKV_AWS_111:KMS key policies require wildcard resources; administration is constrained to this account root principal.
  #checkov:skip=CKV_AWS_356:KMS key policies require wildcard resources for key policy statements.
  statement {
    sid    = "EnableIamPermissionsForAccount"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }

    actions   = ["kms:*"]
    resources = ["*"]
  }
}

resource "aws_s3_bucket" "logs" {
  bucket = "system-sentinel-logs-${random_id.logs_bucket_suffix.hex}"

  tags = {
    Name = "system-sentinel-logs"
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket = aws_s3_bucket.logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.s3_logs.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "expire-old-log-versions"
    status = "Enabled"

    filter {
      prefix = ""
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}
