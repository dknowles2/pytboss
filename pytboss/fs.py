"""Client library for Mongoose OS filesystem RPCs."""

from base64 import b64decode

from .transport import Transport


class FileSystem:
    """Client library for Mongoose OS filesystem RPCs.

    Also see: https://mongoose-os.com/docs/mongoose-os/api/rpc/rpc-service-fs.md
    """

    def __init__(self, conn: Transport) -> None:
        """Initializes the class.

        :param conn: Transport for the device.
        """
        self._conn = conn

    async def get_file_list(self) -> dict | None:
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
            resp = (
                await self._conn.send_command(
                    "FS.Get", {"filename": filename, "offset": offset, "len": length}
                )
                or {}
            )
            content += b64decode(resp["data"])
            offset += length
            if resp["left"] == 0:
                # Decoded once, whole: a multi-byte character split across
                # the chunk boundary is not valid UTF-8 on its own.
                return content.decode("utf-8")

    async def set_file_content(
        self, filename: str, data: str, append: bool
    ) -> dict | None:
        """Writes content to a file on the device.

        :param filename: Path of the file to write.
        :param data: Content to write.
        :param append: If True, appends to the existing file instead of
            overwriting it.
        """
        return await self._conn.send_command(
            "FS.Put",
            {"filename": filename, "data": data, "append": append},
        )

    async def rename_file(self, src: str, dst: str) -> dict | None:
        """Renames a file on the device.

        :param src: Existing filename.
        :param dst: New filename.
        """
        return await self._conn.send_command("FS.Rename", {"src": src, "dst": dst})

    async def delete_file(self, filename: str) -> dict | None:
        """Deletes a file from the device.

        :param filename: Path of the file to delete.
        """
        return await self._conn.send_command("FS.Remove", {"filename": filename})
