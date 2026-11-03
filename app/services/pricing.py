import json
import os
import re
import subprocess
from dataclasses import dataclass

from app.core.config import Settings


@dataclass(frozen=True)
class CostEstimate:
    monthly_usd: float | None
    items: list[str]
    status: str


class AwsPricing:
    """Best-effort AWS Pricing API lookup for common Terraform resources."""

    def __init__(self, settings: Settings, timeout: float = 12) -> None:
        self.settings = settings
        self.timeout = timeout

    def estimate(self, plan_output: str) -> CostEstimate:
        resources = self._resource_specs(plan_output)
        if not resources:
            return CostEstimate(None, [], "no_priced_resources")
        total = 0.0
        items: list[str] = []
        unavailable = False
        for service, filters, label in resources:
            price = self._lookup(service, filters)
            if price is None:
                unavailable = True
                continue
            monthly = price * 730
            total += monthly
            items.append(f"{label}: ≈ ${monthly:,.2f}/month")
        if not items:
            return CostEstimate(None, [], "unavailable")
        return CostEstimate(total, items, "partial" if unavailable else "complete")

    def _resource_specs(self, output: str) -> list[tuple[str, list[tuple[str, str]], str]]:
        location = _aws_location(self.settings.aws_region)
        specs: list[tuple[str, list[tuple[str, str]], str]] = []
        for match in re.finditer(
            r"#\s+(aws_instance\.[^\s]+)\s+will be (?:created|updated in-place)([\s\S]{0,1800}?)"
            r"instance_type\s*=\s*\"([^\"]+)\"",
            output,
            re.IGNORECASE,
        ):
            address, _, instance_type = match.groups()
            specs.append((
                "AmazonEC2",
                [("instanceType", instance_type), ("location", location), ("operatingSystem", "Linux"),
                 ("tenancy", "Shared"), ("preInstalledSw", "NA"), ("capacitystatus", "Used")],
                f"EC2 {address} ({instance_type})",
            ))
        for match in re.finditer(
            r"#\s+(aws_db_instance\.[^\s]+)\s+will be (?:created|updated in-place)([\s\S]{0,2200}?)"
            r"instance_class\s*=\s*\"([^\"]+)\"",
            output,
            re.IGNORECASE,
        ):
            address, _, instance_class = match.groups()
            specs.append((
                "AmazonRDS",
                [("instanceType", instance_class), ("location", location), ("databaseEngine", "PostgreSQL"),
                 ("deploymentOption", "Single-AZ")],
                f"RDS {address} ({instance_class})",
            ))
        if re.search(r"#\s+aws_s3_bucket\.[^\s]+\s+will be (?:created|updated in-place)", output, re.IGNORECASE):
            specs.append((
                "AmazonS3",
                [("location", location)],
                "S3 storage and requests",
            ))
        if re.search(r"#\s+aws_kms_key\.[^\s]+\s+will be (?:created|updated in-place)", output, re.IGNORECASE):
            specs.append((
                "awskms",
                [("location", location)],
                "KMS key",
            ))
        if re.search(r"#\s+aws_nat_gateway\.[^\s]+\s+will be (?:created|updated in-place)", output, re.IGNORECASE):
            specs.append(("AWSNATGateway", [("location", location)], "NAT Gateway"))
        unique: dict[tuple[str, str], tuple[str, list[tuple[str, str]], str]] = {}
        for service, filters, label in specs:
            address = label.split(" (", 1)[0]
            unique[(service, address)] = (service, filters, label)
        return list(unique.values())

    def _lookup(self, service: str, filters: list[tuple[str, str]]) -> float | None:
        args = ["aws", "pricing", "get-products", "--service-code", service, "--region", "us-east-1",
                "--filters", *[f"Type=TERM_MATCH,Field={field},Value={value}" for field, value in filters],
                "--output", "json"]
        env = os.environ.copy()
        if self.settings.aws_access_key_id:
            env["AWS_ACCESS_KEY_ID"] = self.settings.aws_access_key_id
        if self.settings.aws_secret_access_key:
            env["AWS_SECRET_ACCESS_KEY"] = self.settings.aws_secret_access_key
        if self.settings.aws_session_token:
            env["AWS_SESSION_TOKEN"] = self.settings.aws_session_token
        env["AWS_DEFAULT_REGION"] = self.settings.aws_region
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout, env=env, check=False)
            if result.returncode != 0:
                return None
            products = json.loads(result.stdout).get("PriceList", [])
            for raw in products:
                product = json.loads(raw) if isinstance(raw, str) else raw
                for term in product.get("terms", {}).get("OnDemand", {}).values():
                    for dimension in term.get("priceDimensions", {}).values():
                        value = dimension.get("pricePerUnit", {}).get("USD")
                        if value is not None and float(value) > 0:
                            return float(value)
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
            return None
        return None


def _aws_location(region: str) -> str:
    locations = {
        "eu-central-1": "EU (Frankfurt)",
        "us-east-1": "US East (N. Virginia)",
        "us-west-2": "US West (Oregon)",
        "eu-west-1": "EU (Ireland)",
    }
    return locations.get(region, region)
