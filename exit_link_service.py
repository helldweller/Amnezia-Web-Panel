"""Orchestration of exit-node links between two panel servers.

An *entry* AWG instance is linked to an *exit* node in three SSH steps on two
servers: the entry generates its key pair, the exit registers it as a peer,
the entry writes exit0.conf and reroutes its clients. Every step is idempotent
and a failed link is rolled back, so a link is recorded in data.json only
when the tunnel is really up (or the caller insisted with force=True).

Dependencies are injected (like ConnectionService) so the whole flow is unit
tested with fake managers and an in-memory data.json.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime

logger = logging.getLogger(__name__)

EXIT_MTU_LIMIT = 1420          # exit0 MTU; client MTU must not exceed it
HANDSHAKE_WAIT_SECONDS = 15
HANDSHAKE_POLL_SECONDS = 2


class ExitLinkError(Exception):
    """A user-facing failure: `code` is a stable identifier for the UI/i18n."""

    def __init__(self, code, message, status_code=400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def peer_id_for(entry_uid, protocol):
    """Identity of an entry instance on the exit: one link per AWG instance."""
    return f"{entry_uid}:{protocol}"


class ExitLinkService:
    def __init__(self, *, load_data, save_data, data_lock, get_ssh,
                 awg_manager_factory, exit_manager_factory,
                 protocol_base, awg_protocols, protocol_display_name,
                 find_server_by_uid, sleep=None):
        self.load_data = load_data
        self.save_data = save_data
        self.data_lock = data_lock
        self.get_ssh = get_ssh
        self.awg_manager_factory = awg_manager_factory
        self.exit_manager_factory = exit_manager_factory
        self.protocol_base = protocol_base
        self.awg_protocols = awg_protocols
        self.protocol_display_name = protocol_display_name
        self.find_server_by_uid = find_server_by_uid
        self._sleep = sleep or asyncio.sleep
        # Two SSH sessions and no cross-server lock: serialize per (entry, protocol)
        self._locks = defaultdict(asyncio.Lock)

    # ----- read-only helpers (no SSH) -----

    def list_exit_nodes(self, data):
        nodes = []
        for idx, server in enumerate(data.get('servers', [])):
            rec = (server.get('protocols') or {}).get('exit') or {}
            if not rec.get('installed'):
                continue
            nodes.append({
                'uid': server.get('uid', ''),
                'server_id': idx,
                'name': server.get('name') or server.get('host', ''),
                'host': server.get('host', ''),
                'port': rec.get('port'),
                'subnet': rec.get('subnet'),
                'obfuscation': bool(rec.get('obfuscation')),
            })
        return nodes

    def linked_entries(self, data, exit_uid):
        """Every (server, protocol) whose exit_link points at `exit_uid`."""
        entries = []
        if not exit_uid:
            return entries
        for idx, server in enumerate(data.get('servers', [])):
            for proto, rec in (server.get('protocols') or {}).items():
                link = (rec or {}).get('exit_link') or {}
                if link.get('exit_uid') == exit_uid:
                    entries.append({
                        'server_id': idx,
                        'server_uid': server.get('uid', ''),
                        'name': server.get('name') or server.get('host', ''),
                        'protocol': proto,
                        'stale': link.get('stale'),
                    })
        return entries

    # ----- validation -----

    def _entry(self, data, server_id, protocol):
        servers = data.get('servers', [])
        if not isinstance(server_id, int) or server_id < 0 or server_id >= len(servers):
            raise ExitLinkError('server_not_found', 'Server not found', 404)
        server = servers[server_id]
        if self.protocol_base(protocol) not in self.awg_protocols:
            raise ExitLinkError('protocol_not_awg', 'Only AmneziaWG instances can be linked to an exit node')
        rec = (server.get('protocols') or {}).get(protocol)
        if not rec or not rec.get('installed'):
            raise ExitLinkError('protocol_not_installed', f'{protocol} is not installed on this server')
        if not server.get('uid'):
            raise ExitLinkError('entry_no_uid', 'This server has no uid yet; restart the panel once', 500)
        return server, rec

    def _exit(self, data, exit_uid):
        _, server = self.find_server_by_uid(data, exit_uid)
        rec = ((server or {}).get('protocols') or {}).get('exit') or {}
        if not server or not rec.get('installed'):
            raise ExitLinkError('exit_not_found', 'Exit node not found or not installed', 404)
        return server, rec

    def _awg(self, server):
        return self.awg_manager_factory(self.get_ssh(server))

    def _exit_manager(self, server):
        return self.exit_manager_factory(self.get_ssh(server))

    # ----- persistence under the lock, resolved by uid (indices may shift) -----

    async def _update_record(self, entry_uid, protocol, mutate):
        async with self.data_lock:
            data = self.load_data()
            _, server = self.find_server_by_uid(data, entry_uid)
            rec = ((server or {}).get('protocols') or {}).get(protocol)
            if rec is None:
                return None
            mutate(rec)
            self.save_data(data)
            return rec

    # ----- link / unlink -----

    async def _wait_handshake(self, awg, protocol):
        polls = max(1, HANDSHAKE_WAIT_SECONDS // HANDSHAKE_POLL_SECONDS)
        for attempt in range(polls):
            status = await asyncio.to_thread(awg.exit_link_status, protocol)
            if status and status.get('handshake_age') is not None:
                return status
            if attempt < polls - 1:
                await self._sleep(HANDSHAKE_POLL_SECONDS)
        return None

    async def _rollback(self, awg, protocol, exit_manager, peer_id):
        for what, call in (('entry unlink', lambda: awg.exit_unlink(protocol)),
                           ('exit remove_peer', lambda: exit_manager.remove_peer(peer_id=peer_id))):
            try:
                await asyncio.to_thread(call)
            except Exception as err:  # the original error is what the user must see
                logger.warning("exit link rollback: %s failed: %s", what, err)

    async def link(self, entry_server_id, protocol, exit_uid, force=False, dns_via_exit=None):
        data = self.load_data()
        entry, rec = self._entry(data, entry_server_id, protocol)
        exit_srv, exit_rec = self._exit(data, exit_uid)
        if exit_srv.get('uid') == entry['uid']:
            raise ExitLinkError('exit_self', 'A server cannot be its own exit node')
        if self.protocol_base(protocol) == 'awg_legacy' and exit_rec.get('obfuscation'):
            raise ExitLinkError('exit_legacy_obfuscation',
                                'AmneziaWG Legacy can only link to a non-obfuscated exit node')
        previous = rec.get('exit_link') or {}
        if dns_via_exit is None:
            dns_via_exit = bool(previous.get('dns_via_exit'))
        peer_id = peer_id_for(entry['uid'], protocol)
        warnings, log = [], []

        async with self._locks[(entry['uid'], protocol)]:
            awg = self._awg(entry)
            entry_pub = await asyncio.to_thread(awg.exit_prepare_keys, protocol)
            try:
                mtu = int(await asyncio.to_thread(awg._get_mtu, protocol) or 0)
            except (TypeError, ValueError):
                mtu = 0
            ipv6 = bool(await asyncio.to_thread(awg._get_subnet_ipv6_ip, protocol))
            if mtu > EXIT_MTU_LIMIT and not force:
                raise ExitLinkError('exit_mtu_too_big',
                                    f'Client MTU {mtu} exceeds the link MTU {EXIT_MTU_LIMIT}; '
                                    f'lower it in the AWG settings or link anyway')

            # The peer on a previous exit is dropped only after the new link is
            # confirmed (below): until then the old exit is what Repair falls
            # back to if this attempt has to be rolled back.
            switching = bool(previous.get('exit_uid')) and previous['exit_uid'] != exit_uid

            async def rolled_back():
                # The rollback restored direct egress, so a saved link (to the
                # previous exit or to this one) no longer describes reality.
                await self._rollback(awg, protocol, exit_manager, peer_id)
                if previous.get('exit_uid'):
                    reason = 'switch_failed' if switching else 'relink_failed'
                    await self._update_record(entry['uid'], protocol,
                                              lambda r: (r.get('exit_link') or {}).__setitem__('stale', reason))

            exit_manager = self._exit_manager(exit_srv)
            name = f"{entry.get('name') or entry.get('host', '')} / {self.protocol_display_name(protocol)}"
            peer = await asyncio.to_thread(exit_manager.add_peer, peer_id, name, entry_pub)
            link = {
                'exit_uid': exit_uid,
                'exit_name': exit_srv.get('name') or exit_srv.get('host', ''),
                'transit_ip': peer['transit_ip'],
                'subnet_cidr': str(peer.get('subnet') or '10.9.0.0/24').split('/')[1],
                'exit_public_key': peer['public_key'],
                'psk': peer['psk'],
                'endpoint_host': exit_srv['host'],
                'endpoint_port': peer['port'] or exit_rec.get('port'),
                'obfuscation': bool(peer.get('obfuscation')),
                'awg_params': peer.get('awg_params') or {},
                'dns_via_exit': dns_via_exit,
            }
            try:
                log.append(await asyncio.to_thread(awg.exit_link, protocol, link))
            except Exception as err:
                await rolled_back()
                raise ExitLinkError('exit_apply_failed', str(err))

            handshake = await self._wait_handshake(awg, protocol)
            if not handshake:
                if not force:
                    await rolled_back()
                    raise ExitLinkError('exit_no_handshake',
                                        f"No handshake from the exit node within {HANDSHAKE_WAIT_SECONDS} s "
                                        f"(is {link['endpoint_port']}/udp reachable on {link['endpoint_host']}?). "
                                        f"The link was rolled back")
                warnings.append('no_handshake_yet')

            if switching:
                # New link confirmed: drop our peer on the previous exit, best effort.
                try:
                    old_exit, _ = self._exit(data, previous['exit_uid'])
                    await asyncio.to_thread(self._exit_manager(old_exit).remove_peer, None, peer_id)
                except Exception as err:
                    logger.warning("previous exit %s unreachable, peer left behind: %s", previous['exit_uid'], err)
                    warnings.append('old_exit_unreachable_orphan_peer')

            record = {
                'exit_uid': exit_uid,
                'exit_name': link['exit_name'],
                'transit_ip': link['transit_ip'],
                'exit_public_key': link['exit_public_key'],
                'endpoint_host': link['endpoint_host'],
                'endpoint_port': link['endpoint_port'],
                'obfuscation': link['obfuscation'],
                'dns_via_exit': dns_via_exit,
                'linked_at': datetime.now().isoformat(),
                'stale': None,
            }
            await self._update_record(entry['uid'], protocol, lambda r: r.__setitem__('exit_link', record))
        return {'status': 'success', 'exit_link': record, 'ipv6_refused': ipv6,
                'warnings': warnings, 'log': log}

    async def unlink(self, entry_server_id, protocol):
        data = self.load_data()
        entry, rec = self._entry(data, entry_server_id, protocol)
        link = rec.get('exit_link')
        if not link:
            return {'status': 'success', 'warnings': [], 'unlinked': False}
        warnings = []
        async with self._locks[(entry['uid'], protocol)]:
            # Entry first: direct egress must come back even when the exit is dead.
            await asyncio.to_thread(self._awg(entry).exit_unlink, protocol)
            try:
                exit_srv, _ = self._exit(data, link.get('exit_uid'))
                await asyncio.to_thread(self._exit_manager(exit_srv).remove_peer, None,
                                        peer_id_for(entry['uid'], protocol))
            except Exception as err:
                logger.warning("exit %s unreachable on unlink, peer left behind: %s", link.get('exit_uid'), err)
                warnings.append('exit_unreachable_orphan_peer')
            await self._update_record(entry['uid'], protocol, lambda r: r.pop('exit_link', None))
        return {'status': 'success', 'warnings': warnings, 'unlinked': True}

    async def relink_entry(self, entry_server_id, protocol):
        data = self.load_data()
        _, rec = self._entry(data, entry_server_id, protocol)
        link = rec.get('exit_link')
        if not link:
            raise ExitLinkError('not_linked', 'This instance is not linked to an exit node')
        return await self.link(entry_server_id, protocol, link['exit_uid'], force=True,
                               dns_via_exit=bool(link.get('dns_via_exit')))

    async def relink_entries_for_exit(self, exit_uid):
        """After an exit node was reinstalled: re-register every linked entry."""
        results = []
        for item in self.linked_entries(self.load_data(), exit_uid):
            try:
                await self.relink_entry(item['server_id'], item['protocol'])
                results.append({**item, 'status': 'success', 'error': None})
            except Exception as err:
                logger.warning("re-link %s/%s failed: %s", item['name'], item['protocol'], err)
                results.append({**item, 'status': 'error', 'error': str(err)})
        return results

    async def detach_entries_for_exit(self, exit_uid, reason):
        """The exit is gone (uninstalled, deleted, cleared): restore direct
        egress on every linked entry; unreachable ones keep the record marked
        stale so the UI offers Unlink/Repair and the kill-switch keeps
        protecting their clients."""
        results = []
        data = self.load_data()
        for item in self.linked_entries(data, exit_uid):
            entry = data['servers'][item['server_id']]
            try:
                await asyncio.to_thread(self._awg(entry).exit_unlink, item['protocol'])
                await self._update_record(item['server_uid'], item['protocol'], lambda r: r.pop('exit_link', None))
                results.append({**item, 'status': 'success', 'error': None})
            except Exception as err:
                logger.warning("detach %s/%s failed, marking stale: %s", item['name'], item['protocol'], err)
                await self._update_record(item['server_uid'], item['protocol'],
                                          lambda r: (r.get('exit_link') or {}).__setitem__('stale', reason))
                results.append({**item, 'status': 'error', 'error': str(err)})
        return results

    async def forget_entry_peers(self, server):
        """An entry server is deleted or cleared: drop its peers on the exits."""
        results = []
        data = self.load_data()
        for proto, rec in (server.get('protocols') or {}).items():
            link = (rec or {}).get('exit_link') or {}
            if not link.get('exit_uid') or not server.get('uid'):
                continue
            try:
                exit_srv, _ = self._exit(data, link['exit_uid'])
                await asyncio.to_thread(self._exit_manager(exit_srv).remove_peer, None,
                                        peer_id_for(server['uid'], proto))
                results.append({'protocol': proto, 'exit_uid': link['exit_uid'], 'status': 'success'})
            except Exception as err:
                logger.warning("could not drop peer for %s on exit %s: %s", proto, link['exit_uid'], err)
                results.append({'protocol': proto, 'exit_uid': link['exit_uid'], 'status': 'error', 'error': str(err)})
        return results

    async def status(self, entry_server_id, protocol):
        data = self.load_data()
        entry, rec = self._entry(data, entry_server_id, protocol)
        awg = self._awg(entry)
        link_status = await asyncio.to_thread(awg.exit_link_status, protocol)
        try:
            mtu = int(await asyncio.to_thread(awg._get_mtu, protocol) or 0)
        except (TypeError, ValueError):
            mtu = 0
        ipv6 = bool(await asyncio.to_thread(awg._get_subnet_ipv6_ip, protocol))
        return {
            'status': 'success',
            'exit_link': rec.get('exit_link'),
            'exit_link_status': link_status,
            'ipv6': ipv6,
            'mtu': mtu,
            'exit_mtu': EXIT_MTU_LIMIT,
        }
