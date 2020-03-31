#!/bin/env python

import json
import logging
import os
import string
import sys
import time
import boto3
import datetime
from bson import json_util
from pymongo import MongoClient
from pymongo.errors import OperationFailure
from kafka import KafkaProducer                                                               
from elasticsearch import Elasticsearch                                        
import urllib.request                                                    
from base64 import b64decode

"""
Read data from a DocumentDB collection's change stream and replicate that data to MSK.

Required environment variables:
DOCUMENTDB_URI: The URI of the DocumentDB cluster to stream from.
DOCUMENTDB_USR: The user to connect to the DocumentDB cluster to stream from.
DOCUMENTDB_PSW: The password to connect to the DocumentDB cluster to stream from.
DOCUMENTDB_ISODATE: Array that contains ISODate attributes to cast them. 
STATE_COLLECTION: The name of the collection in which to store sync state.
STATE_DB: The name of the database in which to store sync state.
STATE_SYNC_COUNT: How many events to process before syncing state.
WATCHED_COLLECTION_NAME: The name of the collection to watch for changes.
WATCHED_DB_NAME: The name of the database to watch for changes.
MSK_BOOTSTRAP_SRV: The URIs of the MSK cluster to publish messages. 
MSK_TOPIC_NAME: MSK topic name that will host the docdb messages. 
MAX_LOOP: The max for the iterator loop.  
SNS_TOPIC_ARN_ALERT: The topic to send exceptions.    
SNS_TOPIC_ARN_EVENT: The topic to send docdb events.    
BUCKET_NAME: The name of the bucket that will save streamed data. 
BUCKET_PATH: The path of the bucket that will save streamed data. 
ES_INDEX_NAME: The name of the Elasticsearch index where data should be streamed.
ELASTICSEARCH_URI: The URI of the Elasticsearch domain where data should be streamed.
KINESIS_STREAM : The Kinesis Stream name to publish DocumentDB events.

"""

db_client = None
kafka_client = None                                                               
es_client = None     
kinesis_client = None    
s3_client = None                                                                             
sns_client = boto3.client('sns')                 # SNS - used as target and for exception alerting purposes
                                  
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# decryption of DocumentDB password
ENCRYPTED_DOCDB = os.environ['DOCUMENTDB_PSW']
psw_docdb=boto3.client('kms').decrypt(CiphertextBlob=b64decode(ENCRYPTED_DOCDB))['Plaintext']

# The error code returned when data for the requested resume token has been deleted
TOKEN_DATA_DELETED_CODE = 136

def get_db_client():
    """Return an authenticated connection to DocumentDB"""
    # Use a global variable so Lambda can reuse the persisted client on future invocations
    global db_client

    if db_client is None:
        logger.debug('Creating new DocumentDB client.')

        try:
            cluster_uri = os.environ['DOCUMENTDB_URI']
            db_client = MongoClient(cluster_uri)
            # force the client to connect
            db_client.admin.command('ismaster')
            db_client["admin"].authenticate(
                name=os.environ['DOCUMENTDB_USR'], password=str(psw_docdb, 'utf-8'))

            logger.debug('Successfully created new DocumentDB client.')
        except Exception as ex:
            logger.error('Failed to create new DocumentDB client: {}'.format(ex))
            send_sns_alert(str(ex))
            raise

    return db_client


def get_state_collection_client():
    """Return a DocumentDB client for the collection in which we store processing state."""

    logger.debug('Creating state_collection_client.')
    try:
        db_client = get_db_client()
        state_db_name = os.environ['STATE_DB']
        state_collection_name = os.environ['STATE_COLLECTION']
        state_collection = db_client[state_db_name][state_collection_name]
    except Exception as ex:
        logger.error('Failed to create new state collection client: {}'.format(ex))
        send_sns_alert(str(ex))
        raise

    return state_collection


def get_last_processed_id():
    """Return the resume token corresponding to the last successfully processed change event."""

    logger.debug('Returning last processed id.')
    try:
        last_processed_id = None
        state_collection = get_state_collection_client()
        state_doc = state_collection.find_one({'currentState': True})
        if state_doc is not None:
            last_processed_id = state_doc['lastProcessed']
        else:
            state_collection.insert({'dbWatched': str(os.environ['WATCHED_DB_NAME']), 
                'collectionWatched': str(os.environ['WATCHED_COLLECTION_NAME']), 'currentState': True})
    except Exception as ex:
        logger.error('Failed to return last processed id: {}'.format(ex))
        send_sns_alert(str(ex))
        raise

    return last_processed_id


def store_last_processed_id(resume_token):
    """Store the resume token corresponding to the last successfully processed change event."""

    logger.debug('Storing last processed id.')
    try:
        state_collection = get_state_collection_client()
        state_collection.update_one({'dbWatched': str(os.environ['WATCHED_DB_NAME']), 'collectionWatched': str(os.environ['WATCHED_COLLECTION_NAME'])},
            {'$set': {'lastProcessed': resume_token}})
    except Exception as ex:
        logger.error('Failed to store last processed id: {}'.format(ex))
        send_sns_alert(str(ex))
        raise


def connect_kafka_producer():
    """Return a MSK client to publish the streaming messages."""
    # Use a global variable so Lambda can reuse the persisted client on future invocations
    global kafka_client
    
    if kafka_client is None:
        logger.debug('Creating new Kafka client.')

        try:
            kafka_client = KafkaProducer(bootstrap_servers=os.environ['MSK_BOOTSTRAP_SRV'])
        except Exception as ex:
            logger.error('Failed to create new Kafka client: {}'.format(ex))
            send_sns_alert(str(ex))
        raise
    
    return kafka_client


def publish_message(producer_instance, topic_name, key, value):
    """Publish documentdb changes to MSK."""
    # size of the messages  #######################################################
    try:
        logger.debug('Publishing message ' + key + ' to Kafka.')
        key_bytes = bytes(key, encoding='utf-8')
        value_bytes = bytes(value, encoding='utf-8')
        producer_instance.send(os.environ['MSK_TOPIC_NAME'], key=key_bytes, value=value_bytes)
        producer_instance.flush()
    except Exception as ex:
        logger.error('Exception in publishing message: {}'.format(ex))
        send_sns_alert(str(ex))
        raise


def get_es_client():
    """Return an Elasticsearch client."""
    # Use a global variable so Lambda can reuse the persisted client on future invocations
    global es_client
    
    if es_client is None:
        logger.debug('Creating Elasticsearch client Amazon root CA')
        """
            Important:
            Use the following method if you Lambda has access to the Internet, 
            otherwise include the certificate within the package. 
        """
        get_es_certificate()

        try:
            es_uri = os.environ['ELASTICSEARCH_URI']
            es_client = Elasticsearch([es_uri],
                                      use_ssl=True,
                                      ca_certs='/tmp/AmazonRootCA1.pem')
        except Exception as ex:
            logger.error('Failed to create new Elasticsearch client: {}'.format(ex))
            send_sns_alert(str(ex))
            raise

    return es_client


def get_es_certificate():                           
    """Gets the certificate to connect to ES."""
    try:
        logger.debug('Getting Amazon Root CA certificate.')
        url = 'https://www.amazontrust.com/repository/AmazonRootCA1.pem'
        urllib.request.urlretrieve(url, '/tmp/AmazonRootCA1.pem')
    except Exception as ex:
        logger.error('Failed to download certificate to connect to ES: {}'.format(ex))
        send_sns_alert(str(ex))
        raise


def load_data_s3(filename):
    """Load data into S3."""
    # Use a global variable so Lambda can reuse the persisted client on future invocations
    global s3_client

    if s3_client is None:
        logger.debug('Creating new S3 client.')
        s3_client = boto3.client('s3')  

    try:
        logger.debug('Loading batch to S3.')
        response = s3_client.upload_file('/tmp/'+filename, os.environ['BUCKET_NAME'], str(os.environ['BUCKET_PATH']) +
            str(os.environ['WATCHED_DB_NAME']) + '/' + str(os.environ['WATCHED_COLLECTION_NAME']) + '/' + filename)
    except Exception as ex:
        logger.error('Exception in loading data to s3 message: {}'.format(ex))
        send_sns_alert(str(ex))
        raise


def send_sns_alert(message):
    """send an SNS alert"""
    try:
        logger.debug('Sending SNS alert.')
        response = sns_client.publish(
            TopicArn=os.environ['SNS_TOPIC_ARN_ALERT'],
            Message=message,
            Subject='Document DB Replication Alarm',
            MessageStructure='default'
        )
    except Exception as ex:
        logger.error('Exception in publishing alert to SNS: {}'.format(ex))
        send_sns_alert(str(ex))
        raise


def publish_sns_event(message):
    """send event to SNS"""
    try:
        logger.debug('Sending SNS message event.')
        response = sns_client.publish(
            TopicArn=os.environ['SNS_TOPIC_ARN_EVENT'],
            Message=message
        )
    except Exception as ex:
        logger.error('Exception in publishing message to SNS: {}'.format(ex))
        send_sns_alert(str(ex))
        raise


def publish_kinesis_event(pkey,message):
    """send event to Kinesis"""
    # Use a global variable so Lambda can reuse the persisted client on future invocations
    global kinesis_client

    if kinesis_client is None:
        logger.debug('Creating new S3 client.')
        kinesis_client = boto3.client('kinesis')  

    try:
        # size of the messages  #######################################################
        logger.debug('Publishing message' + pkey + 'to Kinesis.')
        message_bytes = bytes(message, encoding='utf-8')
        response = kinesis_client.put_record(
            StreamName=os.environ['KINESIS_STREAM'],
            Data=message_bytes,
            PartitionKey=pkey
        )
    except Exception as ex:
        logger.error('Exception in publishing message to Kinesis: {}'.format(ex))
        send_sns_alert(str(ex))
        raise


def lambda_handler(event, context):
    """Read any new events from DocumentDB and apply them to an streaming/datastore endpoint."""
    events_processed = 0

    try:
        
        # S3 client set up   
        if "BUCKET_NAME" in os.environ:
            filename = str(os.environ['WATCHED_COLLECTION_NAME']) + datetime.datetime.now().strftime("%s") 
            fobj = open('/tmp/'+filename, 'w')
            logger.debug('S3 client set up.')

        # Kafka client set up    
        if "MSK_BOOTSTRAP_SRV" in os.environ:
            kafka_client = connect_kafka_producer()  
            logger.debug('Kafka client set up.')    

        # ElasticSearch target indext set up
        if "ES_INDEX_NAME" in os.environ:
            es_client = get_es_client()
            es_index = os.environ['ES_INDEX_NAME']
            logger.debug('ES client set up.')

        # DocumentDB watched collection set up
        db_client = get_db_client()
        watched_db = os.environ['WATCHED_DB_NAME']
        watched_collection = os.environ['WATCHED_COLLECTION_NAME']
        collection_client = db_client[watched_db][watched_collection]
        logger.debug('Watching collection {}'.format(collection_client))

        # DocumentDB sync set up
        state_sync_count = int(os.environ['STATE_SYNC_COUNT'])
        last_processed_id = get_last_processed_id()
        logger.debug("last_processed_id: {}".format(last_processed_id))

        with collection_client.watch(full_document='updateLookup', resume_after=last_processed_id) as change_stream:
            i = 0

            while change_stream.alive and i < int(os.environ['MAX_LOOP']):
            
                i += 1
                change_event = change_stream.try_next()
                logger.debug('Event: {}'.format(change_event))

                if change_event is None:
                    # On the first function invocation, we must sleep until the first event is processed,
                    # or processing will be trapped in a empty loop having never processed a first event
                    if last_processed_id is None:
                        time.sleep(1)
                        continue
                    else:
                        break
                else:
                    op_type = change_event['operationType']

                    if op_type in ['insert', 'update']:             
                        doc_body = change_event['fullDocument']
                        doc_id = str(doc_body.pop("_id", None))
                        readable = datetime.datetime.fromtimestamp(change_event['clusterTime'].time).isoformat()
                        doc_body.update({'operation':op_type,'timestamp':str(change_event['clusterTime'].time),'timestampReadable':str(readable)})
                        payload = {'_id':doc_id}
                        payload.update(doc_body)

                        # Publish event to ES   ################## evaluate if re-indexing the whole document is the best approach for updates #####################
                        if "ES_INDEX_NAME" in os.environ:
                            es_client.index(index=es_index,id=doc_id,body=json_util.dumps(doc_body))   

                        # Append event for S3 micro-batch
                        if "BUCKET_NAME" in os.environ:
                            fobj.write(json_util.dumps(payload))
                            fobj.write("\n")
                        
                        # Publish event to Kinesis
                        if "KINESIS_STREAM" in os.environ:
                            publish_kinesis_event(str(doc_id),json_util.dumps(payload))

                        # Publish event to MSK
                        if "MSK_BOOTSTRAP_SRV" in os.environ:
                            publish_message(kafka_client, os.environ['MSK_TOPIC_NAME'], str(doc_id), json_util.dumps(payload))

                        # Publish event to SNS
                        if "SNS_TOPIC_ARN_EVENT" in os.environ:
                            publish_sns_event(json_util.dumps(payload))

                        logger.debug('Processed event ID {}'.format(doc_id))

                    if op_type == 'delete':
                        #try:
                        doc_id = str(change_event['documentKey']['_id'])
                        readable = datetime.datetime.fromtimestamp(change_event['clusterTime'].time).isoformat()
                        payload = {'_id':doc_id,'operation':op_type,'timestamp':str(change_event['clusterTime'].time),'timestampReadable':str(readable)}

                        # Delete event from ES
                        if "ES_INDEX_NAME" in os.environ:
                            es_client.delete(es_index, doc_id)

                        # Append event for S3 micro-batch
                        if "BUCKET_NAME" in os.environ:
                            fobj.write(json_util.dumps(payload))
                            fobj.write("\n")

                        # Publish event to Kinesis
                        if "KINESIS_STREAM" in os.environ:
                            publish_kinesis_event(str(doc_id),json_util.dumps(payload))

                        # Publish event to MSK
                        if "MSK_BOOTSTRAP_SRV" in os.environ:
                            publish_message(kafka_client, os.environ['MSK_TOPIC_NAME'], doc_id, json_util.dumps(payload))   

                        # Publish event to SNS
                        if "SNS_TOPIC_ARN_EVENT" in os.environ:
                            publish_sns_event(json_util.dumps(payload))

                        logger.debug('Processed event ID {}'.format(doc_id))

                    events_processed += 1

                    if events_processed >= state_sync_count and "BUCKET_NAME" not in os.environ:
                        # To reduce DocumentDB IO, only persist the stream state every N events
                        store_last_processed_id(change_stream.resume_token)
                        logger.debug('Synced token {} to state collection'.format(change_stream.resume_token))

    except OperationFailure as of:
        send_sns_alert(str(of))
        if of.code == TOKEN_DATA_DELETED_CODE:
            # Data for the last processed ID has been deleted in the change stream,
            # Store the last known good state so our next invocation
            # starts from the most recently available data
            store_last_processed_id(None)
        raise

    except Exception as ex:
        logger.error('Exception in executing replication: {}'.format(ex))
        send_sns_alert(str(ex))
        raise

    else:
        
        if events_processed > 0:

            # S3 - close temp object and load data
            if "BUCKET_NAME" in os.environ:
                fobj.close()
                load_data_s3(filename)

            store_last_processed_id(change_stream.resume_token)
            logger.debug('Synced token {} to state collection'.format(change_stream.resume_token))
            return{
                'statusCode': 200,
                'description': 'Success',
                'detail': json.dumps(str(events_processed)+ ' records processed successfully.')
            }
        else: 
            return{
                'statusCode': 201,
                'description': 'Success',
                'detail': json.dumps('No records to process.')
            }

    finally:

        # S3 - close temp object
        if "BUCKET_NAME" in os.environ:
            fobj.close()

        # Close Kafka client
        if "MSK_BOOTSTRAP_SRV" in os.environ:                                                 
            kafka_client.close()