import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorAnalysis:
    category: str
    title: str
    explanation: str
    recommendation: str
    resource: str | None = None


def analyze_terraform_error(output: str, command: str = "") -> ErrorAnalysis | None:
    text = output.strip()
    if not text:
        return None

    match = re.search(r"(?:Role|User|Policy|Instance Profile)\s+\(([^)]+)\)", text, re.IGNORECASE)
    if not match:
        match = re.search(
            r"(?:creating|updating|deleting)\s+([A-Za-z0-9_./-]+).*?"
            r"(?:EntityAlreadyExists|already exists)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
    if match:
        resource = match.group(1)
        return ErrorAnalysis(
            category="already_exists",
            title="AWS resource already exists",
            explanation=(
                f"AWS rejected the operation because `{resource}` or its underlying "
                "name is already present in this account."
            ),
            recommendation=(
                "Use a unique name, import the existing resource into Terraform, "
                "or remove the old resource only if you are sure it is unused."
            ),
            resource=resource,
        )

    if re.search(r"AccessDenied|UnauthorizedOperation|not authorized", text, re.IGNORECASE):
        return ErrorAnalysis(
            category="permissions",
            title="AWS permissions are insufficient",
            explanation="The configured AWS identity cannot perform one of the requested operations.",
            recommendation=(
                "Check the AWS account and role being used, then grant only the missing "
                "permission or choose a resource configuration allowed by that role."
            ),
        )

    if re.search(r"InvalidAMIID\.NotFound|InvalidAMIID|does not exist", text, re.IGNORECASE):
        return ErrorAnalysis(
            category="region_or_ami",
            title="The selected AMI or region is not available",
            explanation="The AMI is not available in the selected AWS region or no longer exists.",
            recommendation="Refresh the region-specific AMI lookup and run plan again before applying.",
        )

    if re.search(r"DependencyViolation|resource .* in use|still contains", text, re.IGNORECASE):
        return ErrorAnalysis(
            category="dependency",
            title="AWS resource still has dependencies",
            explanation="AWS cannot remove this resource because another resource still refers to it.",
            recommendation="Destroy dependent resources first, or inspect the Terraform graph and state.",
        )

    if re.search(r"RequestLimitExceeded|Throttl", text, re.IGNORECASE):
        return ErrorAnalysis(
            category="throttling",
            title="AWS API rate limit reached",
            explanation="AWS temporarily throttled one of the API requests.",
            recommendation="Wait briefly and retry the operation; reduce parallel changes if it repeats.",
        )

    if re.search(r"timeout|timed out|context deadline exceeded", text, re.IGNORECASE):
        return ErrorAnalysis(
            category="timeout",
            title="Terraform operation timed out",
            explanation="The command did not finish within the configured timeout.",
            recommendation="Check provider/AWS availability and retry. Large plans may need a longer timeout.",
        )

    if re.search(r"no valid credential|InvalidClientToken|SignatureDoesNotMatch", text, re.IGNORECASE):
        return ErrorAnalysis(
            category="credentials",
            title="AWS credentials are invalid or expired",
            explanation="Terraform could not authenticate the AWS request.",
            recommendation="Refresh the AWS credentials or session token and run the plan again.",
        )

    provider_match = re.search(
        r"(?:provider|plugin)[^.\n]*(?:not available|could not be found|"
        r"has no available releases|failed to install)[^\n]*",
        text,
        re.IGNORECASE,
    )
    if provider_match or re.search(r"hashicorp/(?:archive|random|aws)", text, re.IGNORECASE):
        return ErrorAnalysis(
            category="provider",
            title="Terraform provider is unavailable",
            explanation=(
                "Terraform could not install a required provider. The local provider cache "
                "does not contain it, or the registry is unreachable."
            ),
            recommendation=(
                "Check internet access to registry.terraform.io and run Terraform init again. "
                "The agent now uses the shared cache first and falls back to the registry for "
                "providers such as hashicorp/archive."
            ),
        )

    if command.startswith("terraform"):
        return ErrorAnalysis(
            category="terraform",
            title="Terraform could not complete the operation",
            explanation="The command returned an error from Terraform or an AWS provider.",
            recommendation="Review the detailed output below, fix the reported resource or configuration, and create a fresh plan.",
        )
    return None
