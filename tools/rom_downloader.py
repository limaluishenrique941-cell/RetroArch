#!/usr/bin/env python3
"""Download homebrew ROMs linked by PDROMS pages through a local web UI."""
import argparse
import html
import http.server
import ipaddress
import io
import os
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser


PDROMS_HOSTS = {'pdroms.de', 'www.pdroms.de'}
ROM_EXTENSIONS = {
    '.a26', '.a52', '.a78', '.bin', '.c64', '.gb', '.gba', '.gbc', '.gen',
    '.gg', '.int', '.lnx', '.md', '.nds', '.nes', '.pce', '.rom', '.sfc',
    '.smc', '.sms', '.v64', '.vb', '.ws', '.wsc', '.xex', '.z64',
}
ARCHIVE_EXTENSIONS = {'.zip'}
MAX_PAGE_SIZE = 8 * 1024 * 1024
MAX_DOWNLOAD_SIZE = 256 * 1024 * 1024
MAX_ROM_SIZE = 128 * 1024 * 1024


class DownloadError(Exception):
    pass


def normalize_url(url):
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise DownloadError('Porta inválida no link.') from error
    if (parsed.scheme != 'https' or not parsed.hostname or
            parsed.username or parsed.password or port not in (None, 443)):
        raise DownloadError('Somente links HTTPS públicos são aceitos.')
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or '/', parsed.query, ''))


def validate_url(url):
    url = normalize_url(url)
    parsed = urllib.parse.urlsplit(url)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise DownloadError('Não foi possível resolver o servidor do link.') from error
    if not addresses or any(
            not ipaddress.ip_address(address[4][0]).is_global
            for address in addresses):
        raise DownloadError('O link não pode apontar para uma rede privada.')
    return url


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        validate_url(new_url)
        return super().redirect_request(
            request, response, code, message, headers, new_url)


def fetch(url, limit):
    url = validate_url(url)
    opener = urllib.request.build_opener(SafeRedirectHandler())
    request = urllib.request.Request(
        url, headers={'User-Agent': 'RetroArch-Homebrew-Downloader/1.0'})
    try:
        with opener.open(request, timeout=20) as response:
            chunks = []
            total = 0
            while total <= limit:
                chunk = response.read(min(65536, limit - total + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
    except (OSError, urllib.error.URLError) as error:
        raise DownloadError('Falha ao acessar o link: ' + str(error)) from error
    if total > limit:
        raise DownloadError('O arquivo excede o limite de tamanho permitido.')
    return b''.join(chunks)


class LinkParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.links = []
        self.href = None
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href = dict(attrs).get('href')
            self.text = []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href is not None:
            self.links.append((self.href, ' '.join(' '.join(self.text).split())))
            self.href = None
            self.text = []


def filename_from_url(url):
    name = urllib.parse.unquote(
        os.path.basename(urllib.parse.urlsplit(url).path))
    return name or 'download'


def extension_from_url(url):
    return os.path.splitext(urllib.parse.urlsplit(url).path.lower())[1]


def safe_filename(name):
    name = name.replace('\\', '/').split('/')[-1]
    name = ''.join(char for char in name if char.isprintable())
    if name in ('', '.', '..'):
        raise DownloadError('Nome de arquivo inválido.')
    return name


def page_links(data, page_url, is_pdroms):
    parser = LinkParser()
    parser.feed(data.decode('utf-8', 'replace'))
    files = []
    pages = []
    for href, label in parser.links:
        absolute = urllib.parse.urljoin(page_url, href)
        try:
            absolute = normalize_url(absolute)
        except DownloadError:
            continue
        extension = extension_from_url(absolute)
        if extension in ROM_EXTENSIONS or extension in ARCHIVE_EXTENSIONS:
            files.append((absolute, label or filename_from_url(absolute)))
        elif is_pdroms:
            host = urllib.parse.urlsplit(absolute).hostname
            if host not in PDROMS_HOSTS:
                pages.append((absolute, label or host or absolute))
    return files, pages


def archive_members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = []
        for info in archive.infolist():
            name = info.filename.replace('\\', '/')
            extension = os.path.splitext(name.lower())[1]
            if (info.is_dir() or name.startswith('/') or '..' in name.split('/') or
                    extension not in ROM_EXTENSIONS or info.file_size > MAX_ROM_SIZE):
                continue
            members.append(info)
    if not members:
        raise DownloadError('O ZIP não contém ROMs suportadas.')
    if sum(info.file_size for info in members) > MAX_DOWNLOAD_SIZE:
        raise DownloadError('As ROMs selecionáveis excedem o limite permitido.')
    return members


def page(title, content):
    return ('<!doctype html><html lang="pt-BR"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + html.escape(title) + '</title>'
            '<style>body{font:16px sans-serif;max-width:850px;margin:2em auto;'
            'padding:0 1em}input{width:min(100%,650px);padding:.6em}button{'
            'padding:.55em;margin:.25em}li{margin:.7em 0}.error{color:#b00}</style>'
            '<h1>' + html.escape(title) + '</h1>' + content + '</html>')


class DownloaderHandler(http.server.BaseHTTPRequestHandler):
    pages = {}
    files = {}
    archives = {}
    output_directory = '.'

    def respond(self, body, status=200):
        encoded = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(encoded)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path != '/':
            self.respond(page('Não encontrado', '<p>Rota inválida.</p>'), 404)
            return
        content = (
            '<p>Insira uma página de jogo do PDROMS. A ferramenta mostra '
            'downloads diretos encontrados na página ou na fonte oficial.</p>'
            '<form method="post" action="/inspect"><input type="url" name="url" '
            'required placeholder="https://pdroms.de/...">'
            '<button>Buscar downloads</button></form>'
            '<p>Arquivos são salvos em <code>' +
            html.escape(self.output_directory) + '</code>. Apenas HTTPS e '
            'arquivos ZIP são inspecionados.</p>')
        self.respond(page('Downloader de homebrew', content))

    def read_form(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise DownloadError('Requisição inválida.')
        if length < 1 or length > 16384:
            raise DownloadError('Requisição inválida.')
        values = urllib.parse.parse_qs(
            self.rfile.read(length).decode('utf-8', 'replace'))
        return {key: value[0] for key, value in values.items() if value}

    def do_POST(self):
        try:
            form = self.read_form()
            if self.path == '/inspect':
                self.inspect(form.get('url', ''))
            elif self.path == '/download':
                self.download(form.get('token', ''))
            elif self.path == '/save':
                self.save_member(form.get('token', ''), form.get('index', ''))
            else:
                self.respond(page('Não encontrado', '<p>Rota inválida.</p>'), 404)
        except (DownloadError, OSError, zipfile.BadZipFile, ValueError) as error:
            self.respond(page('Falha no download',
                              '<p class="error">' + html.escape(str(error)) +
                              '</p><p><a href="/">Voltar</a></p>'), 400)

    def inspect(self, url):
        normalized = validate_url(url)
        host = urllib.parse.urlsplit(normalized).hostname
        if host not in PDROMS_HOSTS and normalized not in self.pages:
            raise DownloadError('Comece por uma página HTTPS do PDROMS.')
        is_pdroms = host in PDROMS_HOSTS
        data = fetch(normalized, MAX_PAGE_SIZE)
        files, pages = page_links(data, normalized, is_pdroms)
        content = '<p>Links encontrados em <code>' + html.escape(normalized) + '</code>.</p>'
        if files:
            content += '<h2>Downloads encontrados</h2><ul>'
            for file_url, label in files:
                token = secrets.token_urlsafe(18)
                self.files[token] = (file_url, label)
                content += ('<li>' + html.escape(label) +
                            ' <form style="display:inline" method="post" '
                            'action="/download"><input type="hidden" name="token" '
                            'value="' + token + '"><button>Selecionar</button></form></li>')
            content += '</ul>'
        if pages:
            content += '<h2>Fontes oficiais</h2><ul>'
            for source_url, label in pages:
                self.pages[source_url] = True
                content += ('<li>' + html.escape(label) +
                            ' <form style="display:inline" method="post" '
                            'action="/inspect"><input type="hidden" name="url" '
                            'value="' + html.escape(source_url, quote=True) +
                            '"><button>Procurar arquivos</button></form></li>')
            content += '</ul>'
        if not files and not pages:
            content += ('<p>Nenhum link direto para ROM ou ZIP foi encontrado. '
                        'Esta página pode exigir uma etapa manual no site oficial.</p>')
        content += '<p><a href="/">Nova busca</a></p>'
        self.respond(page('Resultados', content))

    def download(self, token):
        if token not in self.files:
            raise DownloadError('Seleção expirada ou inválida.')
        url, _ = self.files[token]
        extension = extension_from_url(url)
        if extension not in ROM_EXTENSIONS and extension not in ARCHIVE_EXTENSIONS:
            raise DownloadError('Tipo de arquivo não suportado.')
        data = fetch(url, MAX_DOWNLOAD_SIZE)
        if extension == '.zip':
            self.show_archive(data)
            return
        name = safe_filename(filename_from_url(url))
        if extension not in ROM_EXTENSIONS:
            raise DownloadError('Somente ROMs e arquivos ZIP são aceitos.')
        path = self.write_file(name, data)
        self.respond(page('Download concluído',
                          '<p>' + html.escape(path) + '</p><p><a href="/">Voltar</a></p>'))

    def show_archive(self, data):
        members = archive_members(data)
        token = secrets.token_urlsafe(18)
        self.archives.clear()
        self.archives[token] = (data, members)
        content = '<p>Escolha uma ROM do arquivo:</p><ul>'
        for index, info in enumerate(members):
            content += ('<li>' + html.escape(info.filename) +
                        ' <form style="display:inline" method="post" action="/save">'
                        '<input type="hidden" name="token" value="' + token + '">'
                        '<input type="hidden" name="index" value="' + str(index) +
                        '"><button>Baixar esta ROM</button></form></li>')
        content += '</ul>'
        self.respond(page('Escolher ROM', content))

    def save_member(self, token, index):
        if token not in self.archives:
            raise DownloadError('Seleção expirada ou inválida.')
        try:
            index = int(index)
        except ValueError as error:
            raise DownloadError('Seleção inválida.') from error
        data, members = self.archives[token]
        if index < 0 or index >= len(members):
            raise DownloadError('Seleção inválida.')
        info = members[index]
        if info.file_size > MAX_ROM_SIZE:
            raise DownloadError('A ROM excede o limite permitido.')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            rom_data = archive.read(info)
        name = safe_filename(info.filename)
        path = self.write_file(name, rom_data)
        self.respond(page('Download concluído',
                          '<p>' + html.escape(path) + '</p><p><a href="/">Voltar</a></p>'))

    def write_file(self, name, data):
        name = safe_filename(name)
        path = os.path.join(self.output_directory, name)
        if os.path.exists(path):
            stem, extension = os.path.splitext(name)
            path = os.path.join(
                self.output_directory, stem + '-' + secrets.token_hex(4) + extension)
        with open(path, 'wb') as output:
            output.write(data)
        return path

    def log_message(self, format_string, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--directory', default=os.path.join(
        os.path.expanduser('~'), 'RetroArch', 'downloads'))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port deve estar entre 1 e 65535')
    os.makedirs(args.directory, exist_ok=True)
    DownloaderHandler.output_directory = os.path.abspath(args.directory)
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', args.port), DownloaderHandler)
    print('Abra http://127.0.0.1:%d/ para começar.' % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
