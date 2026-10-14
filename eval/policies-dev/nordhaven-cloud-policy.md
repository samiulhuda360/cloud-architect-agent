Nordhaven Logistics B.V. — Cloud Usage Policy

Document owner: Group CISO. Classification: Internal. Last review: September 2026.

1 Introduction

This policy describes how Nordhaven teams may use public cloud platforms. It complements the Group Information
Security Policy and the Data Protection Policy. Questions go to the Cloud Centre of Excellence.

2 Residency of data

Personal data of customers, drivers and employees shall be hosted within the European Union. Teams that need an
exception file a request with the Data Protection Officer, who records the decision.

3 Encryption and keys

Databases and object storage holding personal data shall use encryption keys that we hold and control ourselves
(bring your own key) instead of keys managed by the cloud provider. Key rotation happens at least once a year.

4 Connectivity

Public network access shall be disabled on all data services; applications connect through private endpoints in
the Nordhaven landing zone. Developers request temporary access through the privileged access workflow.

5 Resilience

Point-in-time backups of production databases are to be retained for two weeks. Each product team owns its own
recovery runbook and rehearses it once per release cycle.

6 Monitoring

Platform and access logs shall be kept for 180 days so that incidents can be investigated. The security operations
centre receives alerts around the clock.

7 Tagging and ownership

Each resource shall carry the tags "application", "environment" and "owner" so costs and incidents reach the right
team.

8 Exceptions

Any exception to this policy is time-limited and approved by the Group CISO.
