output "vpc_id" {
  description = "ID of the VPC created for system-sentinel."
  value       = aws_vpc.main.id
}

output "private_subnet_ids" {
  description = "Private subnet IDs for container workloads."
  value       = [aws_subnet.private_a.id, aws_subnet.private_b.id]
}

output "ecr_repository_url" {
  description = "ECR repository URL for the system-sentinel Docker image."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name for future scheduled or service workloads."
  value       = aws_ecs_cluster.main.name
}

output "logs_bucket_name" {
  description = "Encrypted S3 bucket for exported or archived application logs."
  value       = aws_s3_bucket.logs.bucket
}
