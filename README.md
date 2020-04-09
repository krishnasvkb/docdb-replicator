# A lambda function to replicate an AWS DocumentDB collection change stream events to different targets

This CloudFormation can be used to deploy a lambda to replicate AWS DocumentDB change streams events to ElasticSearch, Amazon Managed Stream for Kafka (or any other Apache Kafka distro), SNS, AWS Kinesis Streams, and/or S3. S3 replication is done in micro-batches and the rest of the integration are near real-time.  

The lambda function is scheduled using a CloudWatch rule. Everytime the function is triggered it queries the DocumentDB change stream for a specific collection and publish the events in the stream of tha collection to any of the targets enabled. 

Lambdas run within VPC therefore it is necessary that it runs in at least one private subnet that can reach internet using a NAT gateway or privatelinks for the services it needs to integrate with (can be seen in the role created by the cloudformation template). 

Lambda uses documentDB credentials and password is encrypted using KMS.    

The lambda function uses 3 variables to control how many events replicates; users are encourage to tune this variables according to the throughput of the collection. This elements are the lambda timeout that is set to 90 seconds and the following environment varibles:
- MAX_LOOP is a control variable to avoid that the lambda times out in an inconsistent state. This is set to 45. 
- STATE_SYNC_COUNT is a control varible that determines how many iteration should the lambda wait before syncing the resume token. It is meant to reduce IO operations on AWS DocumentDB. This is set to 15.

To enable a target, you need to include a value for its environment varibles within the lambda and add permissions to the lambda role or network accordingly. 

# How to install
0. Enable change streams at collection level. Follow instructions given here: https://docs.aws.amazon.com/documentdb/latest/developerguide/change-streams.html
1. Create a python virtualenv and move files in app folder to it. Install dependencies and zip it (Python runtime used is 3.7.4).
2. Create a second python virtualenv and move files at https://github.com/herbertgoto/lambda-envvar-encrypter/tree/master/app to it. Install dependencies and zip it (Python runtime used is 3.7.4)
3. Upload both zip files to an AWS S3 bucket.
4. Run the CloudFormation template "documentdb_replicator.yaml".
    1. You will need to fill in the parameter for S3 bucket name where the zip files are located. 
    2. You will need to fill in the parameter for S3 key for both zip files.
    3. You will need to fill in the parameter for the subnet(s) for the lambda .
    4. You will need to fill in the parameter for the security groups for the lambda.
    5. You will need to fill in the parameters for the documentDB URI and credentials. 
    6. You will need to fill in the parameters for the documentDB state database and collection. 
    7. You will need to fill in the parameters for the documentDB watched database and collection. 
    8. You will need to fill in the parameters for the state sync count and max loop. 
5. Cloudwatch rule is schedule to run every 5 minutes and is disabled by design, modify according to your needs and enable it. Take also into account the control variables mentioned above. 
6. A SNS topic will be created to send exceptions. 

If you already have the KMS key, you can comment the KMS Resource and instead enable it as a CFN parameter (uncomment this part in the CFN). Review the policies and the custom resource that make use of the KMS and make the changes to use the parameter instead.  

# Environment Varibles for targets

Kafka target environment variables:
- MSK_BOOTSTRAP_SRV: The URIs of the MSK cluster to publish messages. 
- MSK_TOPIC_NAME: MSK topic name that will host the docdb messages. 

SNS target environment variables:
- SNS_TOPIC_ARN_EVENT: The topic to send docdb events.    

S3 target environment variables:
- BUCKET_NAME: The name of the bucket that will save streamed data. 
- BUCKET_PATH: The path of the bucket that will save streamed data. 

ElasticSearch target environment variables:
- ES_INDEX_NAME: The name of the Elasticsearch index where data should be streamed.
- ELASTICSEARCH_URI: The URI of the Elasticsearch domain where data should be streamed.

Kinesis target environment variables:
- KINESIS_STREAM : The Kinesis Stream name to publish DocumentDB events.

# Future work
1. Enable micro-batching for ElasticSearch, MSK, and Kinesis.  
2. Evaluate how to do it for multiple collections different than using one lambda per collection. 
