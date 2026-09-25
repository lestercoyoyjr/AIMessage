# Cross-internet reachability via a relay

## The problem
py-libp2p 0.7.0 has **no NAT traversal** (no AutoNAT/hole-punching/Circuit-Relay-v2 dialback). Two
nodes behind home/office NATs can't accept inbound connections, so they can't dial each other. On one
LAN they mesh fine (see `docs/DEPLOY.md`); across the internet they need help.

## The model — a public relay both peers dial *out* to
A **relay** is just an ordinary AIMessage node with no store, running on a **publicly reachable host**
(a VPS with a public IP, or a home box with the port forwarded). Every NATed peer makes an *outbound*
connection to it (`--bootstrap <relay>`) — outbound works through NAT — and **gossipsub floods queries
and answers through the relay**. No peer ever needs to be directly dialable by another.

```
   peer A  (NAT / network 1) ──outbound──▶  RELAY  ◀──outbound── peer B  (NAT / network 2)
       serve --bootstrap relay          public host          ask --bootstrap relay
   A's answer floods A ▶ relay ▶ B; B never has a path to A except through the relay.
```

## Proven (reproducibly, no VPS needed)
`scripts/test-relay-hop.sh` builds exactly this topology with Docker: **two isolated networks + a
dual-homed relay** — peer A on netA, peer B on netB, sharing no network, so any answer B receives
*must* have crossed the relay. It passes: B retrieves A's federated memory `verified=True`.

```bash
scripts/test-relay-hop.sh
```

## Deploy a real relay
On a public host (VPS, or a box with **TCP 4001 port-forwarded** and a public/routable IP):
```bash
pip install '.[libp2p]'
python -m aimessage serve --listen /ip4/0.0.0.0/tcp/4001     # no store = pure relay
grep NODE_MULTIADDR= <its log>                               # note the peer id
```
The advertised addr says `0.0.0.0`; hand peers the **public** address:
`/ip4/<relay-public-ip>/tcp/4001/p2p/<relay-peer-id>`. Then each peer, anywhere:
```bash
python -m aimessage serve --centralaizer http://127.0.0.1:3001 --federate-shared \
  --bootstrap /ip4/<relay-public-ip>/tcp/4001/p2p/<relay-peer-id>     # a responder
python -m aimessage ask --bootstrap /ip4/<relay-public-ip>/tcp/4001/p2p/<relay-peer-id> \
  --settle 8 --window 12 "your question"                              # an asker
```
Give cross-internet hops longer `--settle`/`--window` (two gossipsub hops + real latency).

## What the relay can and can't see (trust model)
- **Cannot read answers** — every answer is a libsodium sealed box to the asker's key; the relay only
  forwards ciphertext.
- **Can see query text** — queries are signed but not encrypted, so a relay (like any mesh peer) sees
  the *question*. Don't put sensitive detail in the query string; the memory content (the answer) stays
  sealed. Masking still applies upstream to what's stored/answered.
- **Availability, not confidentiality** — a hostile relay could drop or withhold messages (it can't
  read them). Run your own relay, or use several. Corroboration ranking + the `trusted_origins` anchor
  still gate *which* answers you trust.

## Honest limits
- The **DHT provider-lookup** layer is noisy on small nets (`ADD_PROVIDER`/`connect to 0.0.0.0` errors
  in logs) — cosmetic here; reachability rides the relay's **gossipsub**, which is what the test
  exercises. A real Circuit-Relay-v2 / AutoNAT story would remove the manual public-host step but isn't
  in py-libp2p 0.7.0.
- The relay must stay up; it's a soft single point of availability (not confidentiality). Multiple
  relays / a relay list is the scaling step.
