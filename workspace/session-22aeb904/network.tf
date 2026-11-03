data "aws_caller_identity" "current" {}

data "aws_region" "current" {}

#checkov:skip=CKV_AWS_109:KMS key policies require wildcard resources; administration is constrained to this account root principal.
#checkov:skip=CKV_AWS_111:KMS key policies require wildcard resources; administration is constrained to this account root principal.
#checkov:skip=CKV_AWS_356:KMS key policies require wildcard resources; service use is constrained by encryption context.
resource "aws_kms_key" "logs" {
  description             = "KMS key for system-sentinel VPC flow logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.logs_kms.json
}

resource "aws_kms_alias" "logs" {
  name          = "alias/system-sentinel-logs"
  target_key_id = aws_kms_key.logs.key_id
}

data "aws_iam_policy_document" "logs_kms" {
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
      values   = ["arn:aws:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/vpc/system-sentinel-flow-logs"]
    }
  }
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "system-sentinel-vpc"
  }
}

resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "system-sentinel-default-empty"
  }
}

resource "aws_subnet" "private_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.42.1.0/24"
  availability_zone       = "eu-central-1a"
  map_public_ip_on_launch = false

  tags = {
    Name = "system-sentinel-private-a"
  }
}

resource "aws_subnet" "private_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.42.2.0/24"
  availability_zone       = "eu-central-1b"
  map_public_ip_on_launch = false

  tags = {
    Name = "system-sentinel-private-b"
  }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "system-sentinel-private-rt"
  }
}

resource "aws_route_table_association" "private_a" {
  subnet_id      = aws_subnet.private_a.id
  route_table_id = aws_route_table.private.id
}

resource "aws_route_table_association" "private_b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.private.id
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/aws/vpc/system-sentinel-flow-logs"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.logs.arn
}

resource "aws_iam_role" "flow_logs" {
  name               = "system-sentinel-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume_role.json
}

data "aws_iam_policy_document" "flow_logs_assume_role" {
  statement {
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }

    actions = ["sts:AssumeRole"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "system-sentinel-vpc-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    effect = "Allow"

    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams"
    ]

    resources = ["${aws_cloudwatch_log_group.flow_logs.arn}:*"]
  }
}

resource "aws_flow_log" "vpc" {
  iam_role_arn    = aws_iam_role.flow_logs.arn
  log_destination = aws_cloudwatch_log_group.flow_logs.arn
  traffic_type    = "ALL"
  vpc_id          = aws_vpc.main.id

  tags = {
    Name = "system-sentinel-vpc-flow-logs"
  }
}
