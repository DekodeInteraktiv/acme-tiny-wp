#!/usr/bin/env python
import argparse, subprocess, json, os, sys, base64, binascii, time, hashlib, re, copy, textwrap, logging, urllib2
try:
    from urllib.request import urlopen # Python 3
except ImportError:
    from urllib2 import urlopen # Python 2

#DEFAULT_CA = "https://acme-staging.api.letsencrypt.org"
DEFAULT_CA = "https://acme-v01.api.letsencrypt.org"

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.StreamHandler(sys.stdout))
LOGGER.setLevel(logging.INFO)

def get_crt(account_key, csr, wp_url, wp_secret, log=LOGGER, CA=DEFAULT_CA):
    # helper function base64 encode for jose spec
    def _b64(b):
        return base64.urlsafe_b64encode(b).decode('utf8').replace("=", "")

    # parse account key to get public key
    log.debug("Parsing account key...")
    proc = subprocess.Popen(["openssl", "rsa", "-in", account_key, "-noout", "-text"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))
    pub_hex, pub_exp = re.search(
        r"modulus:\n\s+00:([a-f0-9\:\s]+?)\npublicExponent: ([0-9]+)",
        out.decode('utf8'), re.MULTILINE|re.DOTALL).groups()
    pub_exp = "{0:x}".format(int(pub_exp))
    pub_exp = "0{0}".format(pub_exp) if len(pub_exp) % 2 else pub_exp
    header = {
        "alg": "RS256",
        "jwk": {
            "e": _b64(binascii.unhexlify(pub_exp.encode("utf-8"))),
            "kty": "RSA",
            "n": _b64(binascii.unhexlify(re.sub(r"(\s|:)", "", pub_hex).encode("utf-8"))),
        },
    }
    accountkey_json = json.dumps(header['jwk'], sort_keys=True, separators=(',', ':'))
    thumbprint = _b64(hashlib.sha256(accountkey_json.encode('utf8')).digest())

    # helper function make signed requests
    def _send_signed_request(url, payload):
        payload64 = _b64(json.dumps(payload).encode('utf8'))
        protected = copy.deepcopy(header)
        protected["nonce"] = urlopen(CA + "/directory").headers['Replay-Nonce']
        protected64 = _b64(json.dumps(protected).encode('utf8'))
        proc = subprocess.Popen(["openssl", "dgst", "-sha256", "-sign", account_key],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate("{0}.{1}".format(protected64, payload64).encode('utf8'))
        if proc.returncode != 0:
            raise IOError("OpenSSL Error: {0}".format(err))
        data = json.dumps({
            "header": header, "protected": protected64,
            "payload": payload64, "signature": _b64(out),
        })
        try:
            resp = urlopen(url, data.encode('utf8'))
            return resp.getcode(), resp.read(), resp.info()
        except IOError as e:
            return getattr(e, "code", None), getattr(e, "read", e.__str__), getattr(e, "info", None)()

    def _wrap_cert(body):
         return """-----BEGIN CERTIFICATE-----\n{0}\n-----END CERTIFICATE-----\n""".format(
         "\n".join(textwrap.wrap(base64.b64encode(body).decode("utf8"), 64)))

    # find domains
    log.debug("Parsing CSR...")
    proc = subprocess.Popen(["openssl", "req", "-in", csr, "-noout", "-text"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("Error loading {0}: {1}".format(csr, err))
    domains = set([])
    common_name = re.search(r"Subject:.*? CN=([^\s,;/]+)", out.decode('utf8'))
    if common_name is not None:
        domains.add(common_name.group(1))
    subject_alt_names = re.search(r"X509v3 Subject Alternative Name: \n +([^\n]+)\n", out.decode('utf8'), re.MULTILINE|re.DOTALL)
    if subject_alt_names is not None:
        for san in subject_alt_names.group(1).split(", "):
            if san.startswith("DNS:"):
                domains.add(san[4:])

    # get the certificate domains and expiration
    log.debug("Registering account...")
    code, result, info = _send_signed_request(CA + "/acme/new-reg", {
        "resource": "new-reg",
        "agreement": json.loads(urlopen(CA + "/directory").read().decode('utf8'))['meta']['terms-of-service'],
    })
    if code == 201:
        log.debug("Registered!")
    elif code == 409:
        log.debug("Already registered!")
    else:
        raise ValueError("Error registering: {0} {1}".format(code, result))

    # verify each domain
    log.info("Verifying domains...")
    for domain in domains:
        log.debug("Verifying {0}...".format(domain))

        # get new challenge
        code, result, info = _send_signed_request(CA + "/acme/new-authz", {
            "resource": "new-authz",
            "identifier": {"type": "dns", "value": domain},
        })
        if code != 201:
            raise ValueError("Error requesting challenges: {0} {1}".format(code, result))

        # make the challenge file
        challenge = [c for c in json.loads(result.decode('utf8'))['challenges'] if c['type'] == "http-01"][0]
        token = re.sub(r"[^A-Za-z0-9_\-]", "_", challenge['token'])
        keyauthorization = "{0}.{1}".format(token, thumbprint)

        data = {
            'challenges' : [
                {
                    'domain' : domain,
                    'path' : token,
                    'validation' : keyauthorization
                }
            ]
        }
        jsondata = json.JSONEncoder().encode( data )
        headers = {
            'X-WP-ACME-KEY' : wp_secret,
            'Content-Type' : 'application/json'
        }

        url = wp_url + '/wp-json/wp-acme/v1/challenges'

        req = urllib2.Request(url, jsondata, headers)
        rsp = urllib2.urlopen(req)
        content = rsp.read()


        # check that the file is in place
        wellknown_url = "http://{0}/.well-known/acme-challenge/{1}".format(domain, token)
        try:
            resp = urlopen(wellknown_url)
            resp_data = resp.read().decode('utf8').strip()
            assert resp_data == keyauthorization
        except (IOError, AssertionError):
            cleanup(domain, token, wp_url, wp_secret)
            raise ValueError("Tried to setup validation, but couldn't download {0}".format(wellknown_url))

        # notify challenge are met
        code, result, info = _send_signed_request(challenge['uri'], {
            "resource": "challenge",
            "keyAuthorization": keyauthorization,
        })
        if code != 202:
            cleanup(domain, token, wp_url, wp_secret)
            raise ValueError("Error triggering challenge: {0} {1}".format(code, result))

        # wait for challenge to be verified
        while True:
            try:
                resp = urlopen(challenge['uri'])
                challenge_status = json.loads(resp.read().decode('utf8'))
            except IOError as e:
                raise ValueError("Error checking challenge: {0} {1}".format(
                    e.code, json.loads(e.read().decode('utf8'))))
            if challenge_status['status'] == "pending":
                time.sleep(2)
            elif challenge_status['status'] == "valid":
                log.debug("{0} verified!".format(domain))
                cleanup(domain, token, wp_url, wp_secret)
                break
            else:
                raise ValueError("{0} challenge did not pass: {1}".format(
                    domain, challenge_status))

    log.info("Domains verified.")

    # get the new certificate
    log.info("Signing certificate...")
    proc = subprocess.Popen(["openssl", "req", "-in", csr, "-outform", "DER"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    csr_der, err = proc.communicate()
    code, result, info = _send_signed_request(CA + "/acme/new-cert", {
        "resource": "new-cert",
        "csr": _b64(csr_der),
    })
    if code != 201:
        raise ValueError("Error signing certificate: {0} {1}".format(code, result))

    chain_url = re.match("\\s*<([^>]+)>;rel=\"up\"", info['Link']).group(1)
    resp = urlopen(chain_url)
    if(resp.getcode() != 200):
        raise ValueError("Error getting chain certificate: {0} {1}".format(resp.getcode(), resp.read()))

    # return signed certificate and intermediate cert!
    log.info("Certificate signed!")
    return _wrap_cert(result), _wrap_cert(resp.read())

def cleanup(domain, token, wp_url, wp_secret):

    challenges = []

    challenges.append( {
        'domain' : domain,
        'path' : token
    })

    jsondata = json.JSONEncoder().encode( { 'challenges' : challenges } )

    headers = {
        'X-WP-ACME-KEY' : wp_secret,
        'Content-Type' : 'application/json'
    }

    url = wp_url + '/wp-json/wp-acme/v1/cleanup'

    req = urllib2.Request(url, jsondata, headers)
    rsp = urllib2.urlopen(req)
    content = rsp.read()

    return None

def main(argv):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            This script automates the process of getting a signed TLS certificate from
            Let's Encrypt using the ACME protocol. It will need to be run on your server
            and have access to your private account key, so PLEASE READ THROUGH IT! It's
            only ~200 lines, so it won't take long.

            ===Example Usage===
            python acme_tiny_wp.py --account-key ./account.key --csr ./domain.csr --wp-url https://yoursite.com --wp-secret aabbccddeeff > ./signed.crt
            ===================

            ===Example Crontab Renewal (once per month)===
            0 0 1 * * python /path/to/acme_tiny_wp.py --account-key /path/to/account.key --csr /path/to/domain.csr --wp-url https://yoursite.com --wp-secret aabbccddeeff > /path/to/signed.crt 2>> /var/log/acme_tiny.log
            ==============================================
            """)
    )
    parser.add_argument("--account-key", required=True, help="path to your Let's Encrypt account private key")
    parser.add_argument("--csr", required=True, help="path to your certificate signing request")
    parser.add_argument("--wp-url", required=True, help="URL to your WordPress installation")
    parser.add_argument("--wp-secret", required=True, help="The shared secret key from WP ACME")
    parser.add_argument("--quiet", action="store_const", const=logging.ERROR, help="suppress output except for errors")
    parser.add_argument("--ca", default=DEFAULT_CA, help="certificate authority, default is Let's Encrypt")
    parser.add_argument("--chain-file", default=None, help="File to save intermediate cert for chain")
    parser.add_argument("--fullchain-file", default=None, help="File to save full chain")

    args = parser.parse_args(argv)
    LOGGER.setLevel(args.quiet or LOGGER.level)
    signed_crt, chained_crt = get_crt(args.account_key, args.csr, args.wp_url, args.wp_secret, log=LOGGER, CA=args.ca)
    sys.stdout.write(signed_crt)
    if args.chain_file:
        with open(args.chain_file, "w") as chain_file:
            chain_file.write(chained_crt)
    if args.fullchain_file:
        with open(args.fullchain_file, "w") as fullchain_file:
            fullchain_file.write(signed_crt + chained_crt)

if __name__ == "__main__": # pragma: no cover
    main(sys.argv[1:])
