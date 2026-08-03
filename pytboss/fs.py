"""Client library for Mongoose OS filesystem RPCs."""

from base64 import b64decode, b64encode

from .transport import RPCResult, Transport, as_dict


class FileSystem:
    """Client library for Mongoose OS filesystem RPCs.

    Also see: https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-service-fs.md
    """

    def __init__(self, conn: Transport) -> None:
        """Initializes the class.

        :param conn: Transport for the device.
        """
        self._conn = conn

    async def get_file_list(self) -> RPCResult:
        """Lists files present on the device's filesystem."""
        return await self._conn.send_command("FS.List", {})

    async def get_file_content(self, filename: str) -> str:
        """Reads and returns the full text content of a file on the device.

        :param filename: Path of the file to read.
        """
        length = 512
        offset = 0
        content = bytearray()
        while True:
            resp = as_dict(
                await self._conn.send_command(
                    "FS.Get", {"filename": filename, "offset": offset, "len": length}
                )
            )
            content += b64decode(resp["data"])
            offset += length
            if resp["left"] == 0:
                # Decoded once, whole: a multi-byte character split across
                # the chunk boundary is not valid UTF-8 on its own.
                return content.decode("utf-8")

    async def set_file_content(
        self, filename: str, data: str | bytes, append: bool
    ) -> RPCResult:
        """Writes content to a file on the device.

        `data` is base64-encoded before it is sent, which is what the RPC
        expects: the device describes the parameter as `%V` -- Mongoose's
        format specifier for base64-encoded binary -- rather than the `%Q` it
        uses for the plain string alongside it::

            FS.Put  {filename: %Q, offset: %ld, data: %V, append: %B}

        which is the same encoding `get_file_content()` decodes on the way
        back.

        :param filename: Path of the file to write.
        :param data: Content to write. Text is encoded as UTF-8.
        :param append: If True, appends to the existing file instead of
            overwriting it.
        """
        raw = data.encode("utf-8") if isinstance(data, str) else data
        return await self._conn.send_command(
            "FS.Put",
            {
                "filename": filename,
                "data": b64encode(raw).decode("ascii"),
                "append": append,
            },
        )

    async def rename_file(self, src: str, dst: str) -> RPCResult:
        """Renames a file on the device.

        :param src: Existing filename.
        :param dst: New filename.
        """
        return await self._conn.send_command("FS.Rename", {"src": src, "dst": dst})

    async def delete_file(self, filename: str) -> RPCResult:
        """Deletes a file from the device.

        :param filename: Path of the file to delete.
        """
        return await self._conn.send_command("FS.Remove", {"filename": filename})
