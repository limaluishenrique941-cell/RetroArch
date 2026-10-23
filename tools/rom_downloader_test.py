#!/usr/bin/env python3
import io
import socket
import unittest
import zipfile
from unittest import mock

import rom_downloader


class RomDownloaderTest(unittest.TestCase):
    def setUp(self):
        self.dns = mock.patch.object(
            rom_downloader.socket, 'getaddrinfo',
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                          ('8.8.8.8', 443))])
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def test_only_https_public_urls(self):
        self.assertEqual(
            rom_downloader.validate_url('https://pdroms.de/game#download'),
            'https://pdroms.de/game')
        with self.assertRaises(rom_downloader.DownloadError):
            rom_downloader.validate_url('http://pdroms.de/game')

    def test_reject_private_addresses(self):
        with mock.patch.object(
                rom_downloader.socket, 'getaddrinfo',
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                              ('127.0.0.1', 443))]):
            with self.assertRaisesRegex(
                    rom_downloader.DownloadError, 'rede privada'):
                rom_downloader.validate_url('https://pdroms.de/game')

    def test_pdroms_page_finds_source_and_archive_links(self):
        body = (
            '<a href="https://creator.example/game">Official source</a>'
            '<a href="https://pdroms.de/files/game.zip">Game archive</a>'
            '<a href="http://creator.example/bad.zip">Unencrypted</a>')
        files, pages = rom_downloader.page_links(
            body.encode(), 'https://pdroms.de/game', True)
        self.assertEqual(files, [
            ('https://pdroms.de/files/game.zip', 'Game archive')])
        self.assertEqual(pages, [
            ('https://creator.example/game', 'Official source')])

    def test_zip_lists_supported_roms_and_ignores_unsafe_entries(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('game.nes', b'nes')
            archive.writestr('../escape.nes', b'bad')
            archive.writestr('readme.txt', b'text')
        members = rom_downloader.archive_members(stream.getvalue())
        self.assertEqual([member.filename for member in members], ['game.nes'])

    def test_zip_without_rom_fails(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('readme.txt', b'text')
        with self.assertRaisesRegex(
                rom_downloader.DownloadError, 'não contém ROMs'):
            rom_downloader.archive_members(stream.getvalue())

    def test_safe_filename_removes_archive_paths(self):
        self.assertEqual(rom_downloader.safe_filename('../games\\game.nes'),
                         'game.nes')
        with self.assertRaises(rom_downloader.DownloadError):
            rom_downloader.safe_filename('../')


if __name__ == '__main__':
    unittest.main()
