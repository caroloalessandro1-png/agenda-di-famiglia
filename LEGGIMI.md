# Agenda di famiglia

Visite mediche, pagamenti e appuntamenti dei genitori, con promemoria sul telefono.
Un solo container. I dati stanno nella cartella `data/`.

## 1. Avvio
1. In `docker-compose.yml` cambia `PEOPLE`, `FAMILY_PIN` e `VAPID_SUBJECT` (la tua email).
2. `docker compose up -d --build`
3. Controlla che funzioni: `http://IP-DEL-SERVER:8080`

## 2. HTTPS con Caddy (obbligatorio per le notifiche)
Aggiungi a Caddy il blocco di `Caddyfile.esempio`, poi `caddy reload` (o riavvia il container Caddy).
Serve un nome di dominio che punti al tuo server (va bene anche uno gratuito DuckDNS) e le porte 80/443
raggiungibili da Internet, così Caddy ottiene il certificato da solo.
Se non vuoi aprire il server a Internet, puoi usare Tailscale con il suo certificato HTTPS.

## 3. Sul telefono di ogni genitore
1. Apri `https://agenda.tuodominio.it` con Chrome.
2. Menu ⋮ → **Installa app**.
3. Apri l'app e premi **🔔 Attiva i promemoria**: scegli di chi vuoi gli avvisi e consenti le notifiche.

## Quando arrivano gli avvisi
- **Appuntamento nuovo**: subito, appena lo inserisci.
- **Con orario**: la sera prima alle 18:00 e 2 ore prima.
- **Senza orario**: la sera prima alle 18:00 e la mattina stessa alle 8:00.
Gli appuntamenti segnati ✅ Fatto non generano più avvisi. Gli orari si cambiano nel `docker-compose.yml`.

## Sicurezza
I dati sono sanitari: usa un PIN lungo. Dopo 8 PIN sbagliati lo stesso indirizzo viene bloccato per 5 minuti.

## Backup
Copia la cartella `data/` (contiene agenda, telefoni registrati e chiave delle notifiche).
