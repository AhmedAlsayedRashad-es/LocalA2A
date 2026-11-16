"""Feature 3: Ed25519 in pure Python (receiver/a2a_sign.py) against RFC 8032.

Run from the project folder:  python -m unittest discover -s tests -p "test_sign.py" -v
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "receiver") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "receiver"))
import a2a_sign  # noqa: E402

# RFC 8032 section 7.1, TEST 1-3: (secret seed, public key, message, signature)
VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


class Sign(unittest.TestCase):
    # S1
    def test_s1_rfc8032_vectors(self):
        for seed, pub, msg, sig in VECTORS:
            seed, msg = bytes.fromhex(seed), bytes.fromhex(msg)
            self.assertEqual(a2a_sign.public_key(seed).hex(), pub)
            self.assertEqual(a2a_sign.sign(seed, msg).hex(), sig)
            self.assertTrue(a2a_sign.verify(bytes.fromhex(pub), msg, bytes.fromhex(sig)))

    # S2
    def test_s2_verify_false_never_raises(self):
        seed, pub, msg, sig = (bytes.fromhex(x) for x in VECTORS[1])
        other_pub = bytes.fromhex(VECTORS[2][1])
        flipped = bytearray(sig)
        flipped[10] ^= 1
        cases = [(pub, msg + b"x", sig), (pub, msg, bytes(flipped)), (other_pub, msg, sig),
                 (pub, msg, sig[:63]), (pub[:31], msg, sig), (b"", msg, sig), (pub, msg, b""),
                 (None, msg, sig), (pub, msg, None), (b"\xff" * 32, msg, sig), (pub, msg, b"\xff" * 64)]
        for p, m, s in cases:
            self.assertIs(a2a_sign.verify(p, m, s), False, (p, m, s))
        new_seed = a2a_sign.generate_seed()
        self.assertEqual(len(new_seed), 32)
        good = a2a_sign.sign(new_seed, b"release")
        self.assertTrue(a2a_sign.verify(a2a_sign.public_key(new_seed), b"release", good))

    # S3
    def test_s3_standard_library_only(self):
        allowed = {"a2a_sign", "a2a_util", "a2a_ssh"}
        for fn in ("a2a_sign.py", "a2a_util.py"):
            with open(os.path.join(ROOT, "receiver", fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    top = m.split(".")[0]
                    if top in allowed:
                        continue
                    self.assertIn(top, sys.stdlib_module_names, f"{fn} imports {m}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
