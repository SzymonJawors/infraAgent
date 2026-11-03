provider "aws" {
  region = "eu-central-1"

  default_tags {
    tags = {
      Project     = "system-sentinel"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}
