resource "aws_security_group" "ecs_tasks" {
  name        = "system-sentinel-ecs-tasks"
  description = "Restricts system-sentinel ECS task networking"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "Allow HTTPS egress for AWS APIs and image pulls"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = {
    Name = "system-sentinel-ecs-tasks"
  }
}

#checkov:skip=CKV_AWS_109:KMS key policies require wildcard resources; administration is constrained to this account root principal.
#checkov:skip=CKV_AWS_111:KMS key policies require wildcard resources; administration is constrained to this account root principal.
#checkov:skip=CKV_AWS_356:KMS key policies require wildcard resources; service use is constrained by encryption context.
resource "aws_kms_key" "ecs_logs" {
  description             = "KMS key for system-sentinel ECS CloudWatch logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.ecs_logs_kms.json
}

resource "aws_kms_alias" "ecs_logs" {
  name          = "alias/system-sentinel-ecs-logs"
  target_key_id = aws_kms_key.ecs_logs.key_id
}

data "aws_iam_policy_document" "ecs_logs_kms" {
  #checkov:skip=CKV_AWS_109:KMS key policies require wildcard resources; administration is constrained to this account root principal.
  #checkov:skip=CKV_AWS_111:KMS key policies require wildcard resources; administration is constrained to this account root principal.
  #checkov:skip=CKV_AWS_356:KMS key policies require wildcard resources; CloudWatch Logs use is constrained by encryption context.
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

  statement {
    sid    = "AllowCloudWatchLogsUse"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${data.aws_region.current.region}.amazonaws.com"]
    }

    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
      "kms:DescribeKey"
    ]

    resources = ["*"]

    condition {
      test     = "ArnEquals"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/ecs/system-sentinel"]
    }
  }
}

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/aws/ecs/system-sentinel"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.ecs_logs.arn
}

#checkov:skip=CKV_AWS_109:KMS key policies require wildcard resources; administration is constrained to this account root principal.
#checkov:skip=CKV_AWS_111:KMS key policies require wildcard resources; administration is constrained to this account root principal.
resource "aws_kms_key" "ecr" {
  description             = "KMS key for system-sentinel ECR repository"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.ecr_kms.json
}

resource "aws_kms_alias" "ecr" {
  name          = "alias/system-sentinel-ecr"
  target_key_id = aws_kms_key.ecr.key_id
}

data "aws_iam_policy_document" "ecr_kms" {
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

resource "aws_ecr_repository" "app" {
  name                 = "system-sentinel"
  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.ecr.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the most recent 20 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_ecs_cluster" "main" {
  name = "system-sentinel"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_iam_role" "ecs_task_execution" {
  name               = "system-sentinel-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]
  }
}

data "aws_iam_policy_document" "ecs_task_execution" {
  #checkov:skip=CKV_AWS_111:ECR authorization token action only supports wildcard resources.
  #checkov:skip=CKV_AWS_356:ECR authorization token action only supports wildcard resources.
  statement {
    sid    = "EcrAuthorizationToken"
    effect = "Allow"

    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrImagePull"
    effect = "Allow"

    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer"
    ]

    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid    = "WriteEcsLogs"
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents"
    ]

    resources = ["${aws_cloudwatch_log_group.ecs.arn}:*"]
  }
}

#checkov:skip=CKV_AWS_111:ECR authorization token action only supports wildcard resources.
#checkov:skip=CKV_AWS_356:ECR authorization token action only supports wildcard resources.
resource "aws_iam_role_policy" "ecs_task_execution" {
  name   = "system-sentinel-ecs-execution"
  role   = aws_iam_role.ecs_task_execution.id
  policy = data.aws_iam_policy_document.ecs_task_execution.json
}
