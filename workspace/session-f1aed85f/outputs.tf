output "s3_bucket_name" {
  description = "Name of the private S3 bucket."
  value       = aws_s3_bucket.private.bucket
}

output "s3_kms_key_arn" {
  description = "ARN of the KMS key used for S3 bucket encryption."
  value       = aws_kms_key.s3.arn
}
