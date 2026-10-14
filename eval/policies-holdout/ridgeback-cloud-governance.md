Ridgeback Analytics LLC
Cloud Governance Standard (CGS-2), approved by the Architecture Review Board, June 2026

1. Scope
This standard applies to every Azure subscription the company owns, including sandboxes.

2. Regions
Production workloads shall be deployed only to Azure regions in the United States (for example East US 2 and
Central US).

3. Approved services
Only these services are approved for production: Azure Kubernetes Service, Azure Database for PostgreSQL, Azure
Cache for Redis, Azure Key Vault and Azure Monitor.

4. Networking
Customer data must not be reachable over the internet.

5. Logging
Log data must be retained for at least one year.

6. Cost
Monthly Azure consumption may not exceed USD 25,000 per business unit.

7. Tagging
The following tags are mandatory on every resource: CostCenter, Owner, DataClass.

8. Review
Exceptions are reviewed by the Architecture Review Board every quarter.
