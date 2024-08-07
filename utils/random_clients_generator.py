import random


def generate_random_client_ids(num_clients, id_len=4) -> list:
    client_ids = []
    for _ in range(num_clients):
        client_ids.append(''.join(random.sample("abcdefghijklmnopqrstuvwxyz1234567890", id_len)))
    return client_ids 


def generate_random_clients(num_clients, Client) -> dict:
    client_ids = generate_random_client_ids(num_clients)
    clients = {}
    for id in client_ids:
        clients[id] = Client(id)
    return clients
