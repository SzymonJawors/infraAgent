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
            items.append(f"{label}: about ${monthly:,.2f}/month at 730 hours")
        if not items:
            return CostEstimate(None, [], "unavailable")
        return CostEstimate(total, items, "partial" if unavailable else "complete")

    def _resource_specs(self, output: str) -> list[tuple[str, list[tuple[str, str]], str]]:
        location = _aws_location(self.settings.aws_region)
        specs: list[tuple[str, list[tuple[str, str]], str]] = []
        for match in re.finditer(r"aws_instance\.[^\s{]+[\s\S]{0,1800}?instance_type\s*=\s*\"([^\"]+)\"", output):
            instance_type = match.group(1)
            specs.append((
                "AmazonEC2",
                [("instanceType", instance_type), ("location", location), ("operatingSystem", "Linux"),
                 ("tenancy", "Shared"), ("preInstalledSw", "NA"), ("capacitystatus", "Used")],
                f"EC2 {instance_type}",
            ))
        for match in re.finditer(r"aws_db_instance\.[^\s{]+[\s\S]{0,2200}?instance_class\s*=\s*\"([^\"]+)\"", output):
            instance_class = match.group(1)
            specs.append((
                "AmazonRDS",
                [("instanceType", instance_class), ("location", location), ("databaseEngine", "PostgreSQL"),
                 ("deploymentOption", "Single-AZ")],
                f"RDS {instance_class}",
            ))
        if "aws_nat_gateway." in output:
            specs.append(("AWSNATGateway", [("location", location)], "NAT Gateway"))
        return specs

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
