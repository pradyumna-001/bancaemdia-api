"""Run the actual API on the listening socket that the sandbox harness owns.

Usage: python tests/identity/api_server.py <inherited listening socket fd>
"""

import socket
import sys

import uvicorn


def main() -> None:
    # The harness keeps 58000 bound for its whole lifetime, so a restart never races
    # the previous process or an ephemeral client port for the bind. The family is
    # read from the descriptor (uvicorn's --fd would treat a TCP socket as AF_UNIX).
    listener = socket.socket(fileno=int(sys.argv[1]))
    # Raw uvicorn access logging stays disabled for the sandbox subprocess too.
    uvicorn.Server(uvicorn.Config("bancaemdia.main:app", access_log=False)).run(sockets=[listener])


if __name__ == "__main__":
    main()
