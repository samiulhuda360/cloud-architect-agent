# Request for proposal: Falkenhain supplier portal

Falkenhain Feinwerk GmbH, Ulm. Contact: Tom Becker, IT Lead. Responses by 30 November 2026.

## About us

We machine precision parts for agricultural and packaging machinery. Around 400 suppliers in Germany, Austria,
Poland and Italy send us quotes, delivery notes and quality certificates, today by email and through an FTP server
that is twelve years old.

## What we want

A supplier portal where suppliers sign in, upload documents, see purchase orders and confirm delivery dates. Our
buyers use the same portal with their company accounts. Purchase orders come from our ERP; a nightly sync job sends
confirmed delivery dates back to it.

## Users and traffic

We expect about 1,200 monthly active users and up to 150 concurrent users at the start of each month, when
deliveries are planned. Peak load is around 20 requests per second, and 85% of requests are reads.

## Data

The database is about 40 GB today and grows about 30% a year as order history builds up. Supplier documents and
drawings come to roughly 1.5 TB. Quality records must be kept for 10 years. Backups must be kept for 35 days.

## Data protection and compliance

The portal stores names, e-mail addresses and phone numbers of supplier staff, so all data must stay in the
European Union and the design must follow GDPR. We are certified to ISO 27001 and the portal falls inside that
scope.

## Availability

The portal must be available 99.95% of the time. If a region fails we can accept losing up to 15 minutes of data
(RPO) and must be back online within 4 hours (RTO).

## Environments and team

We need development, test and production environments. Our two developers use .NET and our ERP already runs on
SQL Server.

## Budget

Our budget is €3,500 per month for hosting, including all environments.
