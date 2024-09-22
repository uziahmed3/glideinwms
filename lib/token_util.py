# SPDX-FileCopyrightText: 2009 Fermi Research Alliance, LLC
# SPDX-License-Identifier: Apache-2.0

"""
Collection of utility functions for HTCondor IDTOKEN generation and verification.

Functions:
    token_file_expired: Checks if the token file has expired.
    token_str_expired: Checks if the token string has expired.
    simple_scramble: Performs a simple scramble (XOR) of HTCondor data.
    derive_master_key: Derives an encryption/decryption key from a password.
    sign_token: Assembles and signs an IDTOKEN.
    create_and_sign_token: Creates an HTCSS IDTOKEN.
"""

import os
import re
import socket
import sys
import time
import uuid

import jwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from glideinwms.lib import logSupport
from glideinwms.lib.subprocessSupport import iexe_cmd


def token_file_expired(token_file):
    """
    Check the validity of token expiration (`exp`) and not-before (`nbf`) claims.

    This function does not check the token's signature, audience, or other claims.

    Args:
        token_file (Path or str): A file containing a JWT (text file with default encoding expected).

    Returns:
        bool: True if `exp` is in the future or absent, and `nbf` is in the past or absent.
              False otherwise.
    """
    expired = True
    try:
        with open(token_file) as tf:
            token_str = tf.read()
        token_str = token_str.strip()
        return token_str_expired(token_str)
    except FileNotFoundError:
        logSupport.log.warning(f"Token file '{token_file}' not found. Considering it expired.")
    except Exception as e:
        logSupport.log.exception("%s" % e)
    return expired


def token_str_expired(token_str):
    """
    Check the validity of token expiration (`exp`) and not-before (`nbf`) claims.

    This function does not check the token's signature, audience, or other claims.

    Args:
        token_str (str): String containing a JWT.

    Returns:
        bool: True if `exp` is in the future or absent, and `nbf` is in the past or absent.
              False otherwise.
    """
    if not token_str:
        logSupport.log.debug("The token string is empty. Considering it expired.")
        return True
    expired = True
    try:
        decoded = jwt.decode(  # noqa: F841
            token_str.strip(),
            options={"verify_signature": False, "verify_aud": False, "verify_exp": True, "verify_nbf": True},
        )
        expired = False
    except jwt.exceptions.ExpiredSignatureError as e:
        logSupport.log.error(f"Expired token: {e}")
    except jwt.exceptions.DecodeError as e:
        logSupport.log.error(f"Bad token: {e}")
        logSupport.log.debug(f"Faulty token: {token_str}")
    except Exception as e:
        logSupport.log.exception(f"Unknown exception decoding token: {e}")
        logSupport.log.debug(f"Faulty token: {token_str}")
    return expired


def simple_scramble(in_buf):
    """Performs a simple scramble (XOR) on a binary string using HTCondor's algorithm.

    Args:
        in_buf (bytearray): Binary string to be scrambled.

    Returns:
        bytearray: The scrambled binary string.
    """
    DEADBEEF = (0xDE, 0xAD, 0xBE, 0xEF)
    out_buf = b""
    for idx in range(len(in_buf)):
        scramble = in_buf[idx] ^ DEADBEEF[idx % 4]
        out_buf += b"%c" % scramble
    return out_buf


def derive_master_key(password):
    """Derives an encryption/decryption key from an unscrambled HTCondor password.

    Args:
        password (bytes): An unscrambled HTCondor password (bytes-like: bytes, bytearray, memoryview).

    Returns:
        bytes: An HTCondor encryption/decryption key.
    """
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"htcondor",
        info=b"master jwt",
        backend=default_backend(),
    )
    return hkdf.derive(password)


def sign_token(identity, issuer, kid, master_key, duration=None, scope=None):
    """Assembles and signs an IDTOKEN.

    Args:
        identity (str): The identity for which the token is generated.
        issuer (str): The IDTOKEN issuer, typically an HTCondor Collector.
        kid (str): The Key ID.
        master_key (bytes): The encryption key.
        duration (int, optional): Number of seconds the IDTOKEN is valid. Default is infinity.
        scope (str, optional): Permissions the IDTOKEN grants. Default is everything.

    Returns:
        str: A signed IDTOKEN (JWT token).
    """
    iat = int(time.time())
    payload = {
        "sub": identity,
        "iat": iat,
        "nbf": iat,
        "jti": uuid.uuid4().hex,
        "iss": issuer,
    }
    if duration:
        exp = iat + duration
        payload["exp"] = exp
    if scope:
        payload["scope"] = scope

    encoded = jwt.encode(payload, master_key, algorithm="HS256", headers={"kid": kid})
    
    if isinstance(encoded, bytes):
        encoded = encoded.decode("UTF-8")
    return encoded


def create_and_sign_token(pwd_file, issuer=None, identity=None, kid=None, duration=None, scope=None):
    """Creates and signs an HTCondor IDTOKEN.

    Args:
        pwd_file (str): File containing an HTCondor password.
        issuer (str, optional): The issuer of the token. Default is HTCondor TRUST_DOMAIN.
        identity (str, optional): The identity claim. Default is $USERNAME@$HOSTNAME.
        kid (str, optional): Key ID. Default is the file name of the password.
        duration (int, optional): Number of seconds the IDTOKEN is valid. Default is infinity.
        scope (str, optional): Permissions the IDTOKEN grants. Default is everything.

    Returns:
        str: A signed HTCondor IDTOKEN.
    """
    if not kid:
        kid = os.path.basename(pwd_file)
    if not issuer:
        full_issuer = iexe_cmd("condor_config_val TRUST_DOMAIN").strip()
        if not full_issuer:
            logSupport.log.warning(
                "Unable to retrieve TRUST_DOMAIN and no issuer provided: token will have empty 'iss'."
            )
        else:
            split_issuers = re.split(" |,|\t", full_issuer)
            issuer = split_issuers[0]
    if not identity:
        identity = f"{os.getlogin()}@{socket.gethostname()}"
    with open(pwd_file, "rb") as fd:
        data = fd.read()
    password = simple_scramble(data)
    if kid == "POOL":
        password += password
    master_key = derive_master_key(password)
    return sign_token(identity, issuer, kid, master_key, duration, scope)


if __name__ == "__main__":
    kid = sys.argv[1]
    issuer = sys.argv[2]
    identity = sys.argv[3]
    with open(kid, "rb") as fd:
        data = fd.read()
    obfusicated = simple_scramble(data)
    master_key = derive_master_key(obfusicated)
    scope = "condor:/READ condor:/WRITE condor:/ADVERTISE_STARTD condor:/ADVERTISE_SCHEDD condor:/ADVERTISE_MASTER"
    idtoken = sign_token(identity, issuer, kid, master_key, scope=scope)
    print(idtoken)
