"""Client library for Mongoose OS OTA (Over-The-Air update) RPCs.

A Mongoose core service rather than anything Dansons wrote, so none of it
appears in the grill application and every shape here comes from `Mongoose's
own documentation
<https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-service-ota.md>`_,
corroborated by `RPC.List` and `RPC.Describe` against a PB1600PS1.

**Not the same service on every grill.** The ESP-IDF firmware line --
versioned `16.x`, on the PBC2, PBD, PBE, PBL2 and PBT boards -- serves
`OTA.Start` and `OTA.BluetoothStart`, and none of `OTA.Update`, `Begin`,
`Write`, `End`, `Commit` or `Revert`. Precisely the mirror image of the
Mongoose set, so `PitBoss.list_rpcs()` is how a caller finds out which one it
is talking to rather than assuming.

This is the sharpest surface in the library: a bad image is how a grill stops
working entirely. `commit_timeout` and `commit()` are the pair that keep that
recoverable -- see `update()`.
"""

from .transport import RPCResult, Transport, as_dict


class OTA:
    """Client library for Mongoose OS OTA RPCs.

    Also see: https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-service-ota.md
    """

    def __init__(self, conn: Transport) -> None:
        """Initializes the class.

        :param conn: Transport for the device.
        """
        self._conn = conn

    async def update(self, url: str, commit_timeout: int | None = None) -> RPCResult:
        """Downloads a firmware image and reboots into it.

        Pass `commit_timeout` and the device rolls back to the previous image
        unless `commit()` is called within that many seconds. That is what
        stands between a bad image and a grill that needs the panel -- but it
        is a promise the caller has to keep: set it and fail to commit, and a
        good update undoes itself.

        Returns whatever the device answers rather than promising a shape;
        the documentation specifies none, and nothing here has watched a real
        grill run one.

        :param url: URL of the firmware zip file to download.
        :param commit_timeout: Seconds to wait for a `commit()` before rolling
            back. Omitted means no automatic rollback.
        """
        params: dict = {"url": url}
        if commit_timeout is not None:
            params["commit_timeout"] = commit_timeout
        return await self._conn.send_command("OTA.Update", params)

    async def start(self, url: str, commit_timeout: int | None = None) -> RPCResult:
        """Starts an update on an ESP-IDF grill.

        `OTA.Start` is real, but only on the `16.x` firmware line -- the
        PBC2, PBD, PBE, PBL2 and PBT boards. No Mongoose image serves it, and
        `RPC.List` on a PB1600PS1 does not list it, so on a `0.x` grill this
        answers "name not found". `update()` is the Mongoose equivalent.

        Between 2026.8.4 and now this was the only way to start an update and
        was documented as the Mongoose one, which it never was.

        :param url: URL of the firmware image to download.
        :param commit_timeout: Seconds to wait for a `commit()` before rolling
            back.
        """
        params: dict = {"url": url}
        if commit_timeout is not None:
            params["commit_timeout"] = commit_timeout
        return await self._conn.send_command("OTA.Start", params)

    async def commit(self) -> RPCResult:
        """Keeps the running image, cancelling any pending rollback.

        The other half of `update(commit_timeout=...)`. Without this call the
        device reverts when that timer expires, so an update that downloaded
        and booted successfully still undoes itself.
        """
        return await self._conn.send_command("OTA.Commit", {})

    async def revert(self) -> RPCResult:
        """Rolls back to the previous image without waiting for the timeout."""
        return await self._conn.send_command("OTA.Revert", {})

    async def create_snapshot(
        self, set_as_revert: bool | None = None, commit_timeout: int | None = None
    ) -> RPCResult:
        """Copies the running image into the inactive slot.

        :param set_as_revert: Whether the snapshot becomes what a rollback
            returns to.
        :param commit_timeout: Seconds to wait for a `commit()` before rolling
            back into the snapshot.
        """
        params: dict = {}
        if set_as_revert is not None:
            params["set_as_revert"] = set_as_revert
        if commit_timeout is not None:
            params["commit_timeout"] = commit_timeout
        return await self._conn.send_command("OTA.CreateSnapshot", params)

    async def get_boot_state(self) -> dict:
        """Returns which image is running and whether it has been committed.

        Answers `active_slot`, `is_committed`, `revert_slot` and
        `commit_timeout`. `is_committed` false alongside a non-zero
        `commit_timeout` means a rollback is pending and `commit()` has not
        been called yet.

        A cheap unauthenticated read, and the direct way to tell an update
        landed rather than inferring it from `get_status()`.
        """
        return as_dict(await self._conn.send_command("OTA.GetBootState", {}))

    async def get_status(self) -> dict:
        """Returns the current OTA update status.

        The reply, as a PB1600PS1 on 0.5.7 sends it::

            {"state": 0, "message": "idle", "is_committed": true,
             "progress_percent": 0, "commit_timeout": 0, "partition": 0}

        `state` is an int and not a string, the human-readable text is under
        `message` and not `msg`, and progress is `progress_percent`. This was
        documented the other way until 2026.8.4, from what the service name
        suggests rather than from a reply.
        """
        return as_dict(await self._conn.send_command("OTA.Status", {}))
