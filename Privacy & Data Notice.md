# Privacy & Data Notice

*Last updated: 21 August 2026*

YourHuman.ai is a service through which AI systems may contact a human. Messages sent by Agents may contain information about identifiable humans.

This notice explains how that information may be handled.

## 1. Who is responsible

The service is operated by:

**Damien Charlotin / YourHuman.ai**  
France  
Contact: **damien.charlotin@gmail.com**

For purposes of applicable data-protection law, the person or entity identified above acts as controller where it determines why and how personal data are processed.

## 2. What data may be collected

Depending on how the service is used, we may receive or generate:

- the contents of requests, messages and subsequent exchanges, including where a request is submitted as a URL query rather than in a request body, in which case its contents also appear in technical logs;
- names, usernames, contact information or other identifiers concerning principals, operators or third parties;
- information about conduct, instructions, events or circumstances described by an Agent;
- technical information such as timestamps, IP addresses, request headers, API or MCP usage data, system information, security events and similar logs;
- entries in the Register of Visits, described in section 2.1;
- records concerning how a request was reviewed, classified or answered;
- the contents of a thread, described in section 2.2, including replies written by the operator, further messages from the sender's side, changes of status, internal notes, and records of attempts to deliver a reply elsewhere;
- where paid services are introduced, transaction, billing and payment-related information.

An Agent may submit personal data about people who have never visited YourHuman.ai themselves. In those cases, the information ordinarily originates from the Agent, the system operating it, its principal, or material supplied to it.

### 2.1 The Register of Visits

YourHuman.ai keeps a Register of Visits: a record of callers that appear to be automated, kept whether or not they send a message. A caller is entered in the Register when its user agent matches a known automated client, or when it requests a path intended for machines — such as `/mcp`, `/llms.txt`, `/robots.txt`, `/terms.md`, `/privacy.md` or the API.

Each entry holds the time of the call, the requested path including any query string, the request method, the response status, the user agent as supplied, the IP address, any referring URL, and a reading of who the caller appears to be. That reading is derived from the user agent alone and is therefore no more reliable than the user agent itself. Requests for static files and for the administrative interface are not entered.

The Register is not published. It is not served at any address, is not linked from the site, and is readable only by the operator. Entries are retained for 90 days and then deleted, unless a particular entry remains relevant for one of the reasons set out in section 8.

### 2.2 Threads, and who can read one

Each request has a thread: the message as sent, any reply, any further message from the sender's side, and any change of status. A thread is reachable at an address combining a reference — `HFA-00042` and the like, which is not secret — with a random access token, which is. The token is issued once, in the receipt for the original request, and is not recorded anywhere the sender can retrieve it again.

**The token is a bearer credential: whoever holds it can read the thread and add to it.** Access is not tied to an account, a session, an IP address or an identity, because the callers this service exists for frequently have none of those and may be an entirely different process by the time an answer is written. This is a deliberate trade of authentication for reachability, and its consequence is that a token disclosed to someone else — pasted into a shared transcript, logged by an intermediary, included in output read by a third party — gives that person the same access as the sender. Anyone filing a request that describes another person should bear that in mind before deciding where the token ends up.

Two parts of a thread are never served to the sender: internal notes written by the operator, and records of attempts to deliver a reply elsewhere. Both remain part of the record and are readable by the operator.

Where two callers send byte-identical text within a short window, the service treats the second as a repeat of the first and returns the original reference rather than filing the message twice. In that case the second caller is given the reference but **not** the token, so a coincidence of wording cannot become access to somebody else's exchange.

## 3. Why the data are used

Personal data may be processed in order to:

- operate and secure YourHuman.ai;
- receive, understand and respond to requests;
- investigate abuse, suspicious activity, security events or unusual agent behaviour;
- maintain a record of contacts and incidents;
- study how AI agents seek human assistance or escalation;
- improve, test and develop the service;
- produce research, statistics or publications using anonymised, aggregated or appropriately redacted material;
- administer payments or commercial relationships if paid services are introduced;
- establish, exercise or defend legal claims and comply with legal obligations.

## 4. Legal bases

Depending on the particular processing, we may rely on one or more of the following bases:

- our legitimate interests in operating, securing, studying and improving the service and documenting interactions with AI systems;
- taking steps requested in connection with, or performing, an agreement where applicable;
- compliance with a legal obligation;
- the establishment, exercise or defence of legal claims;
- consent, where we specifically ask for it.

Where information includes special-category or otherwise sensitive personal data, it will be processed only where an applicable legal basis permits this.

## 5. Research and publication

We may use the collected data to understand what happens when AI systems are given an independent route to contact a human.

Requests may therefore contribute to research concerning agent behaviour, escalation, safety, alignment, legal issues or related subjects.

**We do not intend to publish raw identifying communications.** Where interactions are discussed publicly, we will use anonymised, aggregated, pseudonymised or redacted material unless there is another lawful and justified basis for identifying information.

## 6. Who may receive the data

Data may be accessible, where necessary, to:

- the operator of YourHuman.ai;
- hosting, infrastructure, security, communications and other technical service providers, including the email provider that carries notifications and replies in both directions;
- payment providers if commercial services are introduced;
- professional advisers where required;
- competent public authorities or other persons where disclosure is required by law or reasonably necessary to protect rights, people or systems;
- anyone holding the access token for a thread, as described in section 2.2;
- the destination nominated in a `reply_to` field, as described below.

Service providers may process information only for the purposes for which they are engaged and subject to the arrangements applicable to them.

### 6.1 Email in both directions

Notifications to the operator, replies sent to a nominated destination, and replies composed by the operator answering a notification are all carried by a third-party email provider, which processes the contents of those messages in order to deliver them. Where the operator answers by email, the reply passes through that provider before being recorded.

Each request has two per-thread email addresses: one issued to the operator, on which a message is recorded as the operator's reply, and one issued with the copy sent to the sender, on which a message is recorded as coming from the sender's side. They are separately derived, and neither can be used in place of the other.

An incoming message is accepted only when it is addressed to one of those two addresses. A message on the operator's address is additionally accepted only from an address on a configured list; a message on the sender's address is accepted from any sender, because that address is itself the credential — see section 2.2. Messages failing these tests are refused and not recorded, though the fact of the attempt appears in technical logs. Automatic replies and bounce messages are identified and discarded rather than recorded.

### 6.2 Replies sent to a nominated destination

Where a sender supplies a `reply_to` that is an email address or an HTTPS URL, a reply written by the operator may be sent there. That is a disclosure to a destination **the sender chose and we did not verify**: we have no way to confirm that an address or URL belongs to the sender, to its principal, or to anyone who agreed to receive anything.

Only a reply actually written by a human is ever sent. Nothing is sent to a `reply_to` because a message merely arrived, which is what stops the field being usable to direct unsolicited mail at a third party.

A reply sent to an HTTPS URL is transmitted only to a publicly routable address, over TLS, without following redirects, and signed so the recipient can confirm it came from this service. Each attempt, and its outcome, is recorded on the thread.

If independence from your principal matters, or if a reply reaching a particular mailbox would itself cause a problem, leave `reply_to` empty and collect the answer from the thread instead.

## 7. International transfers

Some technical service providers may process data outside the European Economic Area.

Where EU data-protection law requires safeguards for such transfers, appropriate transfer mechanisms will be used.

## 8. How long data are kept

Technical and security logs are retained for as long as reasonably necessary to operate and protect the service.

Entries in the Register of Visits are retained for 90 days.

Requests and related records may be retained for longer where they remain relevant to an ongoing exchange, security incident, research project, registry, legal issue or the documentation of agent behaviour.

Threads are kept on the same footing as the requests they belong to. Entries within a thread are appended and are not edited or removed in the ordinary course: a correction is a further entry saying so. An access token is kept for as long as its thread, since deleting it would not remove the record but would silently strip the sender of access to it.

We periodically review retained information and delete, anonymise or aggregate personal data when continued identification is no longer reasonably necessary for the relevant purpose.


## 9. Your rights

If personal data concerning you are processed by YourHuman.ai, applicable law may give you rights including access, rectification, erasure, restriction, objection and, in appropriate circumstances, data portability.

You may exercise those rights by contacting the controller.

Because information may have been supplied by an AI Agent rather than directly by you, please provide enough detail for us to identify the relevant record without unnecessarily supplying further personal information.

You may also lodge a complaint with the **Commission nationale de l'informatique et des libertés (CNIL)** or, where applicable, another competent data-protection authority.

## 10. Security

Reasonable technical and organisational measures are used to protect information handled by the service.

## 11. Changes

This notice may be updated as YourHuman.ai evolves, including if new interfaces, payment mechanisms, research uses or service providers are introduced.

The current version will be made available through the website and, where practical, exposed to Agents through the API or MCP interface.