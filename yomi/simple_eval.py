""" Simple job that reads from a storage bucket """

import os
import httplib2
import json
from google.cloud import storage
from google.oauth2 import service_account

SERVICE_ACCOUNT_KEY_LOCATION = os.environ['SERVICE_ACCOUNT_KEY_LOCATION']
BUCKET_NAME = os.environ['BUCKET_NAME']

BASE_DIR = "gs://{}".format(BUCKET_NAME)
MODELS_DIR = os.path.join(BASE_DIR, 'models')
SELFPLAY_DIR = os.path.join(BASE_DIR, 'data/selfplay')
HOLDOUT_DIR = os.path.join(BASE_DIR, 'data/holdout')
SGF_DIR = os.path.join(BASE_DIR, 'sgf')
TRAINING_CHUNK_DIR = os.path.join(BASE_DIR, 'data', 'training_chunks')


def run():
    """ Talk to GCS. Do some things. """

    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_KEY_LOCATION)
    scoped_credentials = credentials.with_scopes(
        ['https://www.googleapis.com/auth/cloud-platform'])

    storage_client = storage.Client(credentials=credentials)

    bucket = storage_client.get_bucket(BUCKET_NAME)

    # List all the models
    models = bucket.list_blobs('models')
    for m in models:
        print(m.name)
    #  print(json.dumps(resp))


def print_env():
    flags = {
        'BUCKET_NAME': BUCKET_NAME,
        'SERVICE_ACCOUNT_KEY_LOCATION': SERVICE_ACCOUNT_KEY_LOCATION,
    }
    print("Env variables are:")
    print('\n'.join('--{}={}'.format(flag, value)
                    for flag, value in flags.items()))


if __name__ == '__main__':
    print_env()
    run()
