# Lumenvale Health, Inc. HIPAA Security Policy

Policy number SEC-004, revision 3. Effective 1 August 2026. Owner: Chief Technology Officer.

## 1. Scope

This policy applies to all workforce members, contractors and systems that create, receive, maintain or transmit
protected health information (PHI) for Lumenvale's virtual care service.

## 2. Data location

PHI must be stored only in data centers located in the United States. Business associates that store PHI for us
must sign a business associate agreement before any data is shared.

## 3. Approved cloud services

Engineering may only use the following Azure services for PHI workloads:

- Azure App Service
- Azure Database for PostgreSQL
- Azure Blob Storage
- Azure Key Vault
- Azure Monitor and Log Analytics
- Azure Functions
- Azure Front Door
- Azure OpenAI Service
- Azure Backup
- Microsoft Entra External ID

New services are added to this list by the Security Committee after a risk assessment.

## 4. Access and network controls

Systems that store PHI must not expose public endpoints; they are reached through private networking only.
Workforce members sign in with multi-factor authentication, and access is reviewed every quarter.

## 5. Audit controls

Audit logs for systems that store PHI must be retained for six years. Logs are protected from change and reviewed
weekly by the security lead.

## 6. Contingency plan

Database backups must be kept for at least 90 days, with a copy in a second region. The disaster recovery plan is
tested twice a year.

## 7. Asset management

All Azure resources must carry the tags `owner` and `phi`.

## 8. Devices

Laptops use full-disk encryption and lock after five minutes without use.
