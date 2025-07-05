# Migrating Remote Data

Author: Phillip Yu
Spec Status: Approved
Last edited time: June 12, 2025 4:44 PM

# Required Reviewers

*Create Asana tickets for spec reviewers and link them in the corresponding column below. Create the ticket on the corresponding team board for the reviewer and assign to them ([Automation](https://app.asana.com/0/1205644398660644/1205645370385151), [Expansion](https://app.asana.com/0/1204134367996820/1204134454192222), [Experience](https://app.asana.com/0/1205550558375762/1205785668213127), [Foundations](https://app.asana.com/0/1204302235557431/1204304942051779), [Platform](https://app.asana.com/0/1206777069172130/1206777408879410))*

[Required Reviewers](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/Required%20Reviewers%2011b1da993e97818490aef4720d7877fa.csv)

# Problem Summary

Our current storage solution for remote data is expensive and adds excessive load to our DB, which is not in a healthy state. We need to move remote data out of Postgres and into a more suitable storage solution. This doc will cover the engineering solution and approach we believe are necessary to support this migration.

Additional relevant resources:

- ✍️ [Product spec](https://docs.google.com/document/d/13eCrtWExYFUTfcCnFGCABcUqy4ieGXHFIG3-3qjGsfo/edit)

# Background and Current Implementation

## What is remote data?

**Remote data** is a field on every common model that consists of a JSON dump of the data as it was originally returned from the third-party. 

Remote data is used by our customers in a few notable scenarios:

- it is returned directly by our API if `include_remote_data=True` is passed as a query parameter
- it is used by ****field mappings and common model overrides, which essentially store the traversal path and terminal value for a specific value in the remote data

Remote data is stored as a list of objects, where each object contains a key to the remote data path (essentially a unique identifier for the remote data) and the remote data itself. Because remote data is unnormalized, the structure of the `data` blob varies between integrations.

![Remote data is stored as a list of {”path”: …, “data”: …} objects.](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/image.png)

Remote data is stored as a list of {”path”: …, “data”: …} objects.

## How is remote data stored under the hood?

Remote data is currently stored as an arbitrary-length column on every common model table in RDS (Amazon Relational Database Service).

![The definition of the remote_data field in our base Django CommonModel class](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/image%201.png)

The definition of the remote_data field in our base Django CommonModel class

Storing remote data in RDS presents a few problems:

- **DB load.**
    - The DB is in a bad state. Relational DBs like Postgres are not designed for storing large, undefined data that can be up to to several MB in size (see [Appendix C: Largest remote_data values](https://www.notion.so/Appendix-C-Largest-remote_data-values-1211da993e97805d94e2cc240c7b1efb?pvs=21)).
    - The max page size in Postgres is ~8KB, and values larger than ~2KB (like remote data often is; see [Appendix A: Extensive remote data cost analysis](https://www.notion.so/Appendix-A-Extensive-remote-data-cost-analysis-12e1da993e9780ba9f0dff6e7faf8ead?pvs=21) ) are broken down into smaller chunks and stored in a separate system called [TOAST](https://www.postgresql.org/docs/current/storage-toast.html) that adds both storage and performance overhead.
- **Expense.**
    - Storing remote data in RDS is expensive. RDS costs ~$0.15 / GB / month, compared to a lower-cost alternative like standard S3 which costs ~$0.02 / GB / month.
    - RDS charges for input/output per second (as opposed to S3, which charges per request), which is expensive because querying / writing to remote data requires more IOPS than smaller fields. In addition, large remote data can overwhelm the server’s I/O cache, resulting in cache thrashing and even more I/O operations, which adds to both performance overhead and cost.

Because updating remote data is expensive, we currently don’t write to it every time it changes on a model; instead, we only update remote data if:

- a common model field or a field mapping has also changed
- or we explicitly specify that we’d like to forcibly update remote data, either as an input to the blueprint (e.g. via Retool), or in the BP step

As a result, customers who fetch our remote data directly via `?include_remote_data=True` may see potentially outdated information.

## Remote data access patterns

The primary access patterns we currently have for remote data are:

- Loading a single model’s remote data given a common model ID
- Loading the entire remote data blob, for up to 100 items at a time, for a given linked account and set of common model IDs

Notably, we don’t currently support any indexing / searching / filtering with remote data. See [Appendix D: Remote data access patterns](https://www.notion.so/Appendix-D-Remote-data-access-patterns-1211da993e9780b1a62cc2935f9506e9?pvs=21) for a more detailed breakdown.

## More on field mappings

While the primary goal of this project is to help with the strain on our infrastructure caused by remote data, a secondary goal is to unblock certain product use cases (reference the [product spec](https://docs.google.com/document/d/13eCrtWExYFUTfcCnFGCABcUqy4ieGXHFIG3-3qjGsfo/edit) for the full set).

The main product requirement that will influence our eng design is the need to unblock on-demand field mappings, **which will mean we no longer need to perform full resyncs every time a field mapping changes. To support this product requirement, our solution for remote data ideally unblocks the ability for remote data to be constantly up-to-date (i.e. written on every sync) in the future.

## Goals

In order of priority, our final storage solution for remote data should ideally:

- Preserve low latency on common model API endpoints when fetching up to 100 models at a time
- Minimize DB load
- Minimize expense
- Minimize implementation cost
- Unblock the ability for remote data to be updated on every sync

# Proposed Solution

The diagram below shows our final proposal:

- We will use a hybrid storage model for remote data:
    - Small remote data will be stored in **DynamoDB**
    - Large remote data will be stored in **S3**
    - A new column `remote_data_location` on `CommonModel` will indicate where the remote data is stored for a given model
- Reading and writing to remote data will be routed through an intermediary query layer

![remote_data.drawio (3).png](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/remote_data.drawio_(3).png)

Our design philosophy is guided by [KISS](https://en.wikipedia.org/wiki/KISS_principle): keep it simple, and add on additional layers of complexity as we need them.

## Hybrid storage solution

We propose storing remote data smaller than 120KB in DynamoDB and remote data larger than 120KB in S3. We will compress the data using `zstd` to reduce costs.

### Why hybrid?

Two main reasons: latency and cost. 

The vast majority of our remote data is small (see [Appendix A: Extensive remote data cost analysis](https://www.notion.so/Appendix-A-Extensive-remote-data-cost-analysis-12e1da993e9780ba9f0dff6e7faf8ead?pvs=21) ). With a cutoff of 120KB, we estimate we’d only store ~0.05% of objects in S3. This means we can reap the benefits of Dynamo for the vast majority of our objects:

- Significantly better latency than S3 to preserve quick responses for customers. As an exercise, if we need to return 100 objects with remote data from an API call:
    - With S3:
        - A DB request today takes somewhere in the realm of ~150ms
        - An individual S3 request will add an additional ~50-100ms (we’ll recover some of that because the DB request will be faster)
        - Making 100 sequential S3 requests would take up to 10 seconds
        - Parallel fetching (discussed later in this spec) would help significantly, but even with 20 threads, that would still add ~0.5 seconds to the request, not including concurrency overhead
    - With Dynamo:
        - A batch-get request in Dynamo returns up to 100 items and takes ~20-200ms (rough estimate, will need to test to be sure)
- Significantly cheaper reads/writes

We choose to store large objects in S3 because:

- Storage in S3 is significantly cheaper
- Circumvents Dynamo’s doc size limit of 400KB

Note that in the past, we actually tried [migrating remote data to Dynamo](https://www.notion.so/RemoteData-w-DynamoDB-7e9ed6e11ab9439b8515ea763b99d3a6?pvs=21). This effort did not go well because of the doc size limit, but our hybrid approach allows us to avoid this problem. 

**Why 120KB as the cutoff point?**

Some data points from our cost analysis of different cutoff points (see [Appendix A: Extensive remote data cost analysis](https://www.notion.so/Appendix-A-Extensive-remote-data-cost-analysis-12e1da993e9780ba9f0dff6e7faf8ead?pvs=21)):

| **Cutoff point** | **% of objects in S3** | **Est. total cost**  | **Est. total cost (remote data updated on every sync)** |
| --- | --- | --- | --- |
| 5KB | 7.5% | $1,256 / month | $7,902 / month |
| 10KB | 0.75% | $1,642 / month | $9,069 / month |
| 40KB | 0.16% | $1,743 / month | $9,536 / month |
| 120KB | 0.058% | $1,793 / month | $9,785 / month |
| 200KB | 0.038% | $1,816 / month | $9,901 / month |
| 400KB | 0.015% | $1,864 / month | $10,139 / month |

Call-outs:

- We think 120KB strikes a reasonable balance between latency and cost.
- In a future world where remote data is updated on every sync, the costs are pretty similar across different cutoff points.

### Cost analysis

Our estimated costs per solution are (all costs are per month):

|  | **Storage cost** | **Read cost** | **Write cost** | **Migration cost** | **Est. total cost (year one)** | **Est. total cost (remote data updated on every sync, year one)**  |
| --- | --- | --- | --- | --- | --- | --- |
| **All RDS (current status quo)** | $2,885 | $1,762 | $6,393 | $0 | $11,041 / month | $72,213 / month |
| **All S3 (original proposal)** | $172 | $56 | $2,554 | $34,023 | $5,618 / month | $30,055 / month |
| **Dynamo + S3 hybrid**  | $1,744 | $23 | $1,456 | $19,393 | $4,839 / month | $20,881 / month |
| **All S3 with compression** | $96 | $56 | $2,554 | $34,023 | $5,541 / month | $29,979 / month |
| **Dynamo + S3 hybrid with compression (new proposal)** | $943 | $16 | $835 | $11,127 | $2,721 / month | $10,713 / month |

Call-outs:

- The hybrid approach with compression is the cheapest by a significant margin
- Storage in S3 is cheap but writes are expensive, so the migration cost is very high
- Storage with the hybrid approach is more expensive, but writes are cheap, so the migration cost is low
    - But, write cost scales more aggressively than storage cost, so hybrid is still cheapest overall
- In a future where we need to write remote data on every common model write, the hybrid solution scales far better

See [Appendix A: Extensive remote data cost analysis](https://www.notion.so/Appendix-A-Extensive-remote-data-cost-analysis-12e1da993e9780ba9f0dff6e7faf8ead?pvs=21) for a more detailed breakdown of this cost analysis.

### Compression

Because storage, read, and write cost all scale with data size with the hybrid approach, compressing remote data on write and decompressing on read will allow us to save significantly on costs.

We propose using `zstd` with a compression level of 6. We chose this algorithm after benchmarking latency and compression performance for several popular algorithms. Selected sample for a 10KB string: 

| **Algorithm** | **Compression ratio** | **Compression latency** | **Decompression latency** |
| --- | --- | --- | --- |
| `gzip` | 1.883 | 0.120ms | 0.026ms |
| `zstd` (level 3) | 1.899 | 0.023ms | 0.027ms |
| `zstd` (level 6, proposed) | 1.934 | 0.073ms | 0.014ms |
| `zstd` (level 9) | 1.935 | 0.127ms | 0.014ms |
| `zstd` (level 15) | 1.945 | 0.337ms | 0.012ms |

The spreadsheet in [Appendix A: Extensive remote data cost analysis](https://www.notion.so/Appendix-A-Extensive-remote-data-cost-analysis-12e1da993e9780ba9f0dff6e7faf8ead?pvs=21) has a more detailed breakdown of our compression metrics.

### New column: `remote_data_location`

We’ll add a new column `remote_data_location` on `CommonModel` to indicate where the remote data is stored for a given model:

```python
remote_data_location = CharField(
        blank=True,
        null=True,
        choices=zip(REMOTE_DATA_LOCATIONS, REMOTE_DATA_LOCATIONS),  # can be DYNAMO, S3, or DOES_NOT_EXIST
        max_length=100,
    )
```

Call-outs:

- A null `remote_data_location` indicates a model whose remote data has not been migrated yet, while a `DOES_NOT_EXIST` value indicates a model that has no remote data. After the migration, we can make this column `null=False` with `default=DOES_NOT_EXIST`.
- On every write to remote data, we will check the size of the new remote data we’re writing. Based off the size, we’ll write to either Dynamo or S3, and update the value of `remote_data_location`. Pseudocode to get size:
    
    ```python
    def get_remote_data_size(self, old_remote_data, new_remote_data):
    	  if old_remote_data is None: # no previous remote data
    	      return size(new_remote_data)
        if new_remote_data.path not in old_remote_data: # new path
            return size(new_remote_data) + size(old_remote_data)
        else: # replacing path
            old_remote_data_without_path_to_update = old_remote_data.remove_data_with_path(new_remote_data.path)
            return size(new_remote_data) + size(old_remote_data_without_path_to_update) 
    ```
    
- On every read to remote data, we’ll pull from the appropriate storage source based off the value of this column.

### Moving data between S3 and Dynamo

We need a strategy to move items between S3 and Dynamo when they cross the cutoff threshold of 120KB. If the remote data to write exceeds the cutoff threshold, and the remote data currently sits in Dynamo, we’ll write it to S3 and delete the existing row from Dynamo. 

Similarly, if a remote data blob falls below the threshold, we’ll write it to Dynamo and delete the existing file from S3. 

We’ll implement a 10% “buffer” around the cutoff threshold for objects that switch storage services, to prevent flapping around the threshold:

- Only switch from Dynamo → S3 if the remote data exceeds 132KB
- Only switch from S3 → Dynamo if the remote data falls below 108KB

Pseudocode:

```python
def upsert_remote_data(self, common_model: CommonModel):
    previous_location = common_model.remote_data_location
    current_remote_data = self.get_remote_data(common_model)
    
    # Check S3 vs Dynamo cutoff
    # We'll need to adjust this logic slightly to account for the 10% buffer above
    if self.get_remote_data_size(current_remote_data, new_remote_data) > SIZE_CUTOFF: 
        self.upsert_remote_data_s3(common_model)
        common_model.remote_data_location = "S3"
        if previous_location == "Dynamo": # if was in Dynamo and moving to S3, need to delete from Dynamo
	          self.delete_remote_data_dynamo(common_model)
    else:
	      self.upsert_remote_data_dynamo(common_model)
	      common_model.remote_data_location = "Dynamo"
        if previous_location == "S3": # if was in S3 and moving to Dynamo, need to delete from S3
	          self.delete_remote_data_s3(common_model)
    
    if previous_location != common_model.remote_data_location:
		    common_model.save()
    
def batch_upsert_remote_data(self, common_models: CommonModel):
    # Similarly, branch on remote_data_location to determine how to write data
    # Make sure to bulk_update remote_data_location if it changed
```

### Alternatives considered

- **Standard S3 only**
    - Migrating all remote data to standard S3 only was the original proposal in V1 of this spec.
    - As discussed earlier, we decided against it because of concerns around cost (see [Cost analysis](https://www.notion.so/Cost-analysis-12e1da993e978080b0b0ccf3f5a44509?pvs=21)) and latency.
- **Standard S3 only, with batching of multiple remote data files together**
    - Instead of having 1 remote data file per model, we could instead combine remote data for multiple models into a single file, using some deterministic field (like the model’s created-at date) to perform the aggregation.
    - This would save on request costs for new models, since we’d be writing multiple remote data objects in a single request.
    - We decided against this approach because:
        - The benefits on reads and updates are limited. We’d probably need to add a distributed caching layer to help with multiple workers reading and writing from the same file.
        - It adds a large amount of engineering and storage complexity, especially with the distributed cache above.
- **Amazon S3 Express One Zone only**
    - [**Amazon S3 Express One Zone**](https://aws.amazon.com/s3/storage-classes/express-one-zone/) is a relatively new S3 service that offers single-millisecond latency for most operations (10x faster than standard S3), at the cost of more expensive storage.
    - Express One Zone is stored and replicated only within a single availability zone, meaning in the case of catastrophic failure we could lose all our remote data. In addition, as a result of being stored in a single AZ, it is designed for 99.95% availability as opposed to 99.99% for standard S3.
    - Because remote data is mission-critical, and only a very small percentage of items will be stored in S3, I don’t recommend using Express One Zone for now. But I think it is a viable storage alternative we can explore further (perhaps duplicating the data in an infrequent-access S3 service) if the need arises.
    - The Amazon S3 Console provides an [easy way to migrate from standard S3 to S3 Express One Zone](https://docs.aws.amazon.com/AmazonS3/latest/userguide/create-import-job.html) using S3 Batch Operations.
- **DynamoDB only**
    - As mentioned earlier, we tried this approach in the past and it did not go well due to the maximum doc size of 400KB. Millions of our existing remote data records exceed this size, which means we would need to build a TOAST-like multi-doc system in order to support large remote data.
- **Hybrid Postgres + S3.**
    - Instead of hybrid Dynamo + S3, we could support a hybrid RDS + S3 approach, where we keep small remote data in RDS, and offload large remote data to S3.
    - Because a large percentage of remote data is small, this would allow us to preserve low latency for the majority of our remote data while only needing to make higher-latency API calls for large remote data.
    - We did not choose this approach due to cost (see [Appendix A: Extensive remote data cost analysis](https://www.notion.so/Appendix-A-Extensive-remote-data-cost-analysis-12e1da993e9780ba9f0dff6e7faf8ead?pvs=21) ), and because it does not help as much as the proposed approach with reducing load on our RDS DB.
- **ElasticSearch (ES).**
    - ElasticSearch is a document-oriented DB specifically optimized for search and filtering. We use it currently for our logs!
    - However, it is considerably more expensive than S3, so we’d likely want to store only portions of remote data in ES, depending on product requirements. In addition, implementation cost is far higher than S3, both due to innate complexity and unfamiliarity with the technology on the team.
    - While we currently do not support searching or filtering on remote data, and don’t have near-term plans to support that, it’s likely we’ll want that ability in the future. When that happens, ElasticSearch will be a good candidate for remote data!
- **Elastic File System (EFS).**
    - EFS is a fully managed, scalable file system service. It provides latency in the single millisecond range (~10x faster than S3, which averages around 50-100ms for common operations).
    - However, EFS is considerably more expensive than S3 (~$0.30 per GB/month). Also, according to David, AWS architects don’t recommend EFS as the sole persistent storage layer.
- **Keeping it in RDS.**
    - We could keep remote data in RDS, and try alternate strategies to lessen DB load. For example, we could split remote data into a separate table, similar to how we’re spinning off the large [logging tables](https://www.notion.so/Merge-Backend-DB-Split-1051da993e9780228869ffdb3ac317fb?pvs=21) into their own DB.
    - While this would help in the short-term, we’re not addressing the fundamental problem of RDS being a sub-optimal storage solution for remote data.

## DynamoDB for small files

### Table schema

We will store remote data <120KB in Dynamo with the following item structure:

```json
{
   "model_id_and_content_type": "common_model_id#common_model_content_type"  // "Main" partition key
   "linked_account_id": "uuid_of_linked_account",  // Partition key of GSI
   "organization_id": "uuid_of_organization",  // Sort key of GSI
   "remote_data": { /* the full remote data JSON blob */ }
}
```

Notable call-outs:

- We’ll use a combination of `{common_model_id}#{common_model_content_type}` as the partition key, to better distribute traffic across partitions
- We need no sort key, since the partition key uniquely identifies the item
- We’ll define one GSI:
    - `{organization_id}` as the primary key
    - `{linked_account_id}` as the sort key
    - This GSI will be used to query all remote data for a given linked account / org, which will be useful when deleting objects (see [Deleting objects](https://www.notion.so/Deleting-objects-1211da993e9780abaca8f0c8e91a6f3e?pvs=21))
- The `remote_data` attribute will contain the full JSON blob, as currently stored in RDS

**Alternatives considered**

We considered other primary key patterns:

- `linked_account_id` as the partition key, and `common_model_id` as the sort key. While this allows us to easily group by `linked_account_id`, this grouping isn’t an access pattern we currently need. In addition, this partition key creates the potential for partition “hot spots”, since some linked accounts receive far more traffic than others.
- `{common_model_id}` as the partition key. We didn’t choose this because there’s a (extremely tiny) chance that two UUIDs collide. Adding the content type to the key avoids this problem, since primary key uniqueness is enforced for each common model table in Postgres.
- `{linked_account_id}#{common_model_id}` as the partition key. There’s no real benefit AFAIK to including the linked account ID in the partition key, since the partition key is hashed and Dynamo does not support substring querying on the partition key. Also, this approach would require us to add a new field on every single item.

### Reading + Writing

We’ll use `boto3` with batch reads + writes to operate over items in bulk. Dynamo natively supports batch operations that are more efficient than individual requests, with latency generally in the 10ms - 50ms range. 

Pseudocode:

```python
def get_remote_data_dynamo(self, common_model: CommonModel):
    response = self.dynamodb.get_item(Key={'common_model_id': self._construct_dynamo_object_key(common_model)})
    return response.get('Item')
    
def batch_get_remote_data_dynamo(self, common_models: list[CommonModel]):
		keys = [self._construct_dynamo_object_key(common_model) for common_model in common_models]
    response = self.dynamodb.batch_get_item(
        RequestItems={
            'RemoteDataStore': {
                'Keys': keys
            }
        }
    )
    return response['Responses'].get('RemoteDataStore', [])
```

### File contents

The `remote_data` attribute will contain the full JSON blob, as currently stored in RDS. 

Instead of storing the entire remote data JSON blob together, we considered splitting up each remote data path into a separate item. Most remote data consists of only a single path (see [Appendix A: Extensive remote data cost analysis](https://www.notion.so/Appendix-A-Extensive-remote-data-cost-analysis-12e1da993e9780ba9f0dff6e7faf8ead?pvs=21) ). We decided against this because:

- The cost difference between the two approaches is negligible
- Reading remote data is slower and more complicated, since we need to fetch and combine multiple items
- We don’t save on writes, since we still need to fetch the remote data to check if it has changed before writing

## S3 for large files

We’ll store remote data items >120KB in our existing `merge-api-{tenant}` S3 bucket using the **S3 Standard** storage class**.** We chose S3 Standard because it is cheap, durable, scales basically infinitely, and has low implementation cost.

### **Object key naming**

S3 object keys are stored in a flat namespace, though they conventionally follow a file system-like naming scheme with prefixes and delimiters. S3 recommends distributing traffic across multiple prefixes to improve performance. 

We propose the following naming structure for our remote data keys:

```
remote_data/
	{organization_id}/
		{linked_account_id}/
			{common_model_content_type, e.g. "hris_employee"}/
				{common_model_id}.json
```

The `linked_account_id` folder distinguishes between separate accounts, the `common_model_content_type` divides our prefix space in a natural way within each account, and the `common_model_id` file name identifies a predictable way to load the remote data for a specific common model.

This predictable file structure means we can eliminate the `remote_data` column in our RDS tables, and can instead just generate the relevant S3 URLs in the application layer when loading the remote data for a given object. Pseudocode:

```python
def _construct_s3_object_key(common_model: CommonModel):
    # Constructs the S3 object key from a common model instance
    linked_account_id = common_model.linked_account_id
    organization_id = common_model.linked_account.organization_id  # adds a JOIN, so be careful to avoid n+1's during implementation
    content_type = ContentType.objects.get_for_model(common_model)
    common_model_id = common_model.id
    return "/".join(["remote_data", organization_id, linked_account_id, content_type.app_label + "_" +
                     content_type.model, common_model_id + ".json"])
```

**File contents**

To minimize migration pain and implementation cost, each remote data object will be the compressed form of a JSON that follows the same shape as existing remote data.

**Alternatives considered**

- Time-based partition strategies (e.g. `/remote_data/{year}/{month}/{day}/`) are common for analytics use cases, and are easily supported by streaming services like Firehose. However, for remote data, the access pattern we care about is direct retrieval / upload given a common model instance. We don’t care about time-based aggregations or querying (unlike CMM or BP executions), so time-based partitioning is not a good fit.

### Fetching data

As discussed above in [Remote data access patterns](https://www.notion.so/Remote-data-access-patterns-11f1da993e978016a2d8c96c0ea8bd4c?pvs=21), we’ll need to load remote data for up to 100 objects in a single page. Unfortunately, since S3 does not natively support batch-fetch API calls, increased latency (compared to RDS or Dynamo) is a concern. In the absolute worst case, where all 100 objects are large and stored in S3, we’ll need to make (up to) 100 separate API calls to fetch that data.

We should implement parallel fetching of multiple objects to improve performance, and can use Python’s `concurrent.futures` package to make concurrent `GetObject` calls. Pseudocode:

```python
def get_remote_data_s3(self, common_model: CommonModel):
    return self.s3.get_object(Bucket=self.bucket_name, Key=self._construct_s3_object_key(common_model))
        
def batch_get_remote_data_s3(self, common_models: list[CommonModel]):
    # We'll need to tune the exact number of threads to use
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(self.get_remote_data, common_model) for common_model in common_models]
        results = [future.result() for future in futures]
    return results
```

### Writing Data

For simplicity, we’ll use direct API requests with `boto3` to write to remote data. Similar to fetching data, we should use the `concurrent.futures` package to make parallel `PutObject` calls when batching writes. Pseudocode:

```python
def upsert_remote_data_s3(self, common_model: CommonModel, remote_data_value: list):
    return self.s3.put_object(Bucket=self.bucket_name, Key=self._construct_s3_object_key(common_model), Body=remote_data_value)
        
def batch_upsert_remote_data_s3(self, common_models_to_remote_data_values: dict[CommonModel, list]):
    # We'll need to tune the exact number of threads to use
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(self.upsert_remote_data, common_model, remote_data_value) for common_model, remote_data_value in common_models_to_remote_data_values.items()]
        results = [future.result() for future in futures]
    return results
```

**Alternatives considered**

- **Amazon Data Firehose** (or another streaming service).
    - Firehose is a fully managed service that supports large-scale streaming of data into S3 (and other destinations). We currently use Firehose to [stream CommonModelMetadata](https://www.notion.so/Mini-Spec-Data-Pipelines-Merge-7a608ad3057449bba79c6ab23e49d19d?pvs=21) into S3. Firehose automatically supports batching and is priced based off data ingestion volume rather than per-request, which can be better when writing many small files a day.
    - However, Firehose only supports up to 500 [dynamic partitions](https://docs.aws.amazon.com/firehose/latest/dev/dynamic-partitioning.html) in buffer at a time. If our partition key uses a smaller grain like linked account ID, this limit becomes a concern as we scale.
    - In addition, Firehose buffers incoming streaming data to a certain size or for a certain period of time before writing it to S3 under a system-generated file name. This makes retrieval of the data more difficult, since multiple remote data blobs will be combined in the same S3 file. We’d need to use a service like Athena to query the remote data for a specific common model ID (our main access pattern), which would add cost, overhead, and latency.
- **Distributed queue for writes.**
    - Instead of writing directly to S3, we could send tasks to a queue, and spin up workers to pull tasks of the queue to write to S3. This decouples writes from the S3 upload process, which could better handle spikes in write volume.
    - This feels like over-engineering for our use case to start — we don’t expect write volume to be a major traffic bottleneck, at least until we support remote data writes on every sync.

## Intermediate application layers

### **Remote data query layer**

Because remote data is no longer a column on our common model tables, it will no longer be accessible directly through the Django ORM via the `.remote_data` property. We propose defining an intermediate `RemoteDataQueryLayer` that will centralize all writes and reads to remote data. This separate layer also makes it easier to swap out the underlying implementation of remote data if we migrate to a different storage solution in the future. 

Pseudocode:

```python
import boto3
from concurrent.futures import ThreadPoolExecutor
from django.contrib.contenttypes.models import ContentType

class RemoteDataQueryLayer(DjangoQueryLayerBase[RemoteData]):
    class Meta:
        # We'll need to play around with this syntax, since RemoteData isn't a Django model
        model = RemoteData

    def __init__(self):
        self.s3 = boto3.client('s3')
        self.dynamodb = boto3.client('dynamodb')
        self.bucket_name = settings.AWS_REMOTE_DATA_STORAGE_BUCKET_NAME  # remote-data-specific bucket

    def get_remote_data(self, common_model: CommonModel):
		    location = common_model.remote_data_location
        if location == "DOES_NOT_EXIST":
            return None
        elif location == "DYNAMO":
            return self.get_remote_data_dynamo(common_model)  # defined in pseudocode earlier
        elif location == "S3":
            return self.get_remote_data_s3(common_model)  # defined in pseudocode earlier
        elif location is None:
            raise("Remote data has not been migrated yet")
        else:
            raise("Invalid remote data location")
    
    def batch_get_remote_data(self, common_models: CommonModel):
		    # Similarly, branch on remote_data_location to determine how to load data
    
    def upsert_remote_data(self, common_models: CommonModel):
	      # Defined in pseudocode earlier
		    
    def batch_upsert_remote_data(self, common_models: CommonModel):
		    # Defined in pseudocode earlier
	
		# Define all the private S3/Dynamo getters and setters below...
```

**Servicing common model API requests**

We’ll need to add some code to `ExpandableSerializer` to instantiate this query layer and call `batch_get_remote_data` when servicing common model API requests for which `?include_remote_data=True`.

**Failure handling**

Because remote data now lives in a separate system, it’s possible for remote data to fall “out of sync” with the rest of the common model stored in RDS if a call to S3/Dynamo fails.

We’ll start with a super simple retry system - if a call fails, we’ll retry again for a small, fixed number of times. If those calls fail too, we’ll generate a Sentry log and move on. We will keep a pulse on errors during rollout, and can calibrate and improve on this failure handling system if it proves a problem.

### Updating the runner cache

**Writes**

Conceptually, we only want to write remote data to S3/Dynamo whenever a common model is successfully written to RDS. This is relevant in the runner cache, which batch-writes common models updated by BPs, and includes logic to handle multiple BPs colliding when writing to the same model.

We’ll need to update the runner cache to write to remote data in the `.commit` function, but ONLY if the transaction is successfully committed, and only for those models for which `remote_data` is intentionally updated. Pseudocode in the cache:

```python
class BPRCacheRemoteDataValue:
    def __init__(self, should_write: bool, remote_data_value: list):
        self.should_write = should_write
        self.remote_data_value = remote_data_value

class BPRCache:
		def __init__(...):
		    # ... existing stuff ...
		    # Common model unique identifier -> the cached remote data value
		    self._remote_data_cache: dict[str, BPRCacheRemoteDataValue] = {}
				self._remote_data_query_layer = RemoteDataQueryLayer()
		
		def write(self, remote_data_value, ...):
				# ... existing stuff ...
				should_write = "remote_data" in updated_fields  # may need to tweak this condition
				self._remote_data_cache[unique_id] = RemoteDataCacheValue(
					should_write=should_write, remote_data_value=remote_data_value
				)						
		
		def commit(self, replace_many_to_many: bool = False) -> None:
				# ... existing stuff...
				
				with transaction.atomic():
				    # ... existing stuff that bulk-writes to Postgres ...
				
				    # After transaction commits successfully, write all remote data values where should_write=True to S3/Dynamo
				    transaction.on_commit(lambda: self.remote_data_query_layer.batch_upsert_remote_data(...))
```

Similarly, we’ll have to add logic to the cache’s `_manual_backout_commit` (which handles the aforementioned BP collisions) to write remote data through the `RemoteDataQueryLayer` after a `.save()`.

**Reads**

Remote data is almost always grabbed directly from an API request’s return value when being written:

![image.png](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/image%202.png)

In rare cases, it is read from an existing model, and then manipulated in the BP ([example](https://admin.merge.dev/integrations/5fef46e0-8678-41f9-af23-4727a088e61d/blueprints/versions/4547a3b8-b8d6-4f67-ba02-a721fb35754e/editor/main)):

![image.png](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/image%203.png)

Since the vast majority of our BP fetch steps have no need to read remote data, we don’t need to load remote data on every single cache read — that’s a lot of unnecessary API calls to S3/Dynamo!

We propose not returning remote data from our standard BP fetch steps, and instead defining a separate step type that can be used to explicitly fetch remote data in the few cases we need it. This separate step type will then call into the query layer as needed. Pseudocode:

```python
	class BPRCache:
			# Called by the new step to explicitly read remote data
			def read_remote_data(self, common_model):
					key = common_model.unique_identifier
					if key in self._remote_data_cache:
							return self._remote_data_cache[key].remote_data_value
					else:
							remote_data_value = self._remote_data_query_layer.get_remote_data(common_model)
							self._remote_data_cache[key] = RemoteDataCacheValue(
								should_write=False, remote_data_value=remote_data_value
							)
							return remote_data_value
```

We’ll write a BP Analyzer script to find existing usages of `.remote_data` in BPs, and manually migrate those to use the new step. 

### Alternative solutions

Instead of re-using the in-memory runner cache, we considered adding a more substantial caching layer with **ElastiCache**, which is a fully managed, distributed cache service that is easy to stand up in front of S3. We use ElastiCache already (with Redis as the underlying caching engine) for rate limits.

However, we decided against using ElastiCache for a few reasons:

- It’s not common enough for multiple workers to be writing to / reading remote data from the same common models for us to benefit much from the distributed nature of ElastiCache, which is one of its main selling points.
- Adds additional expense, overhead, and complexity to the system. Another layer means more failure modes and bugs, especially since we would have to worry about keeping ElastiCache in sync with the runner cache.

## Other considerations

### Deleting objects

We need to make sure to clean up remote data S3 files when their corresponding models are deleted:

- When an organization is deleted, we’ll delete all files under the corresponding `{organization_id}` prefix
- When a linked account is deleted, we’ll delete all files under the corresponding `{linked_account_id}` prefix
- When we hard-delete a specific model instance, we’ll hook into the common model `post_delete` hook (which is called for both singular and bulk deletes) and delete the corresponding file in S3/Dynamo

### Handling multiple updates to remote data

We don’t do a good job today of handling a scenario where two BPs are both writing to remote data in the runner cache. Our manual backout logic doesn’t append remote data, so the later write to remote data “wins”.

We’ll resolve this issue as a part of this project. We need to add logic in the manual backout in the cache to:

- Detect if both workers are writing to remote data for the same model by checking `updated_fields`
- If so, combining both remote data payloads
- … unless both write to the same path, in which case the latter write “wins”

### Tooling

eg Looker, Retool to view remote data

TODO: Figure out how this will work - probably Retool?

# Migration and rollout strategy

Please read @Ashlee Kupor’s mini-spec: [[MiniSpec] Remote data migration](https://www.notion.so/MiniSpec-Remote-data-migration-1261da993e9780b09c81e52bd6f4a2b0?pvs=21) 

# Risks

Migrating remote data will be a significant endeavor, since it involves a paradigm shift in how remote data is accessed:

- We need to make sure the entire suite of remote data features (field mappings, common model overrides, raw remote data, redaction, scopes, etc…) still work after this change.
- We don’t want to significantly degrade existing API performance.
- S3 and Dynamo have different pricing schemes than RDS. We envision that this migration will reduce costs, but should be careful to not accidentally blow up costs.

Our main mitigation for these risks will be to roll out this change very gradually. We will carefully monitor cost, performance, and bugs, and adjust our solution depending on the problems we encounter. See [Migration and rollout strategy](https://www.notion.so/Migration-and-rollout-strategy-1201da993e978038a543d17e21849a16?pvs=21) for more!

## Implementation Plan

We will be tracking all work in [this Asana project](https://app.asana.com/0/1208430331855683/1208430331855683). Duplicated below (on 10/29) for convenience:

| **Task** | **Estimate** | **Team** |
| --- | --- | --- |
| **Scoping** |  |  |
| Eng Spec | 10 days | Expansion |
| **S3 is set up to store remote data** |  |  |
| Set up new Dynamo table in Terraform | 1 days | Foundations |
| Connect merge-backend to new Dynamo table | 0.5 days | Expansion |
| Add new column remote_data_location to CommonModel | 1 days | Expansion |
| **Remote data query layer is complete** |  |  |
| Set up RemoteDataQueryLayer | 2 days | Expansion |
| Implement simple retry system in case S3 calls fail | 1 days | Expansion |
| Update ExpandableSerializer to batch-fetch remote data when necessary | 1 days | Expansion |
| **Runner cache updates are complete** |  |  |
| Write remote data cache when transaction is committed | 1 days | Expansion |
| Handle remote data writes during a manual backout | 1 days | Expansion |
| Analyzer script to pull which BPs reference .remote_data | 1 days | Expansion |
| Define new step type for reading remote data | 1 days | Expansion |
| Migrate existing BPs to use the new step type for reads | 2 days | Expansion (consultation w Platform) |
| **Migration + rollout are complete** |  |  |
| Migrate all existing instances of .remote_data to go through the new query layer | 3 days | Expansion |
| Double-read from RDS and S3 | 1 days | Expansion |
| Double-write to S3 and RDS | 1 days | Expansion |
| Opportunistically backfill remote data | 1 days | Expansion |
| Backfill remote data from RDS -> S3 | 4 days | Expansion |
| **Code complete!** |  |  |
| Delete corresponding S3 file(s) when an org/linked account/object is deleted | 1 days | Expansion |
| Add logic to runner cache to handle multiple remote data updates | 0.5 days | Expansion |
| Set up Looker / Retool to be able to view remote data | 3 days | Expansion |
| E2E testing | 3 days | Expansion |
| **[Foundations] TOTAL** | **1 days** |  |
| **[Expansion] TOTAL (excluding spec)** | **~29 days** |  |

# Product Checklist

[Product Checklist](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/Product%20Checklist%2011b1da993e9781448d73e23a61ce6c64.csv)

# Appendix

### Appendix A: Extensive remote data cost analysis

@Ashlee Kupor put together a master spreadsheet of different costs and remote data metrics, linked at the top of this doc: [Remote Data Migration #s and Cost](https://www.notion.so/Remote-Data-Migration-s-and-Cost-1281da993e97802c98dbf79e0cfb913a?pvs=21) 

A few interesting numbers to call out, besides the ones already included in this spec:

- Scale of read/write operations:
    - We perform ~17 million writes to remote data a day
        - vs ~177 million writes to all common models
    - We perform ~4.5 million reads from remote data a day
- An estimated 61% of remote data has only one path, ~5.5% has two paths, 0.33% has three paths, and 0.00% have four+ paths. The rest are NULL.
- We store about ~15TB of data in RDS. We estimate about half of that (~7.5TB) is remote data.
- Remote data size is strongly right-skewed. An estimated:
    - ~33% is NULL
    - 15% is <1 KB
    - 25% is 1-2 KB
    - 9.1% is 2-3 KB
    - 4.2% is 3-4 KB
    - 4.0% is 4-5 KB
    - 6.5% is 5-10 KB
    - 2.6% is 10-20 KB
    - 0.4% is 20-120 KB
    - 0.04% is 120-200 KB
    - 0.02% is 200-400 KB
    - 0.02% is 400+ KB

### Appendix B: Largest tables by size

The 20 largest tables by size, as of March 2024 ([source](https://www.notion.so/CommonModelMetaData-Re-Architecture-61f8744f381f4d44bb53f85f05264f17?pvs=21)):

![image.png](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/image%204.png)

### Appendix C: Largest remote_data values

- For `ats_application`, the largest `remote_data` values are about ~2MB, as of 10/14:
    
    ![image.png](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/image%205.png)
    
- For `crm_task`, the largest `remote_data` values are about ~100KB, as of 10/14:
    
    ![image.png](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/image%206.png)
    
- For `hris_employee`, the largest remote_data values are about ~2MB, as of 10/14. `hris_employee` has [notoriously large remote data](https://mergeapi.slack.com/archives/C01H00ANG77/p1730301209734609), according to Platform (Workday, SAP success factors, etc.):
    
    ![image.png](Migrating%20Remote%20Data%2011b1da993e9780b0b279cda660cabcc5/image%207.png)
    

### Appendix D: Remote data access patterns

Remote data is accessed in a few notable places:

- When `include_remote_data=True` is passed as a query parameter to our common model endpoints, the entire remote data blob is returned on every model in that page (up to 100 per page)
- When listing the possible field mapping options for a given common model, we grab 250 recently-updated instances of that common model type and list out all traversal paths that point to values in those instances’ remote data
- When a step writes to remote data, we grab all applicable field mappings, traverse through the remote data for that model using the traversal path stored on the mapping, and update the denormalized field mapping value with the terminal value at the end of the traversal
- Similar to the above, when a step writes to remote data, we grab all relevant redaction paths (which are generated statically from the BP JSON), traverse the remote data along all those paths, and redact the terminal values at the end of the traversal

### Appendix E: Further yapping on field mappings

We have a product opinion that field mapping fields should behave the same as common model fields, which means when changed, they should update the modified-at value on the associated common model when changed. 

We currently get modified-at updates for “free” because the field mapping value is denormalized onto a separate column every time remote data is written; in the future, though, we’d like to move field mapping fields that are calculated *on-demand,* which allows us to no longer perform full resyncs every time a field mapping changes. To support this product requirement, remote data ideally needs to:

- be updated on every sync
- continue to update the modified-at value on a common model when a field mapping value is changed

Notes:

[12/17 chat with david](https://www.notion.so/12-17-chat-with-david-15e1da993e9780519a77c5dfb57d2ca6?pvs=21)

[10/21 Remote Data Spec Review](https://www.notion.so/10-21-Remote-Data-Spec-Review-1261da993e978063804acf08dfc1322f?pvs=21) 

[12/02 Remote Data Rollout Meeting Notes](https://www.notion.so/12-02-Remote-Data-Rollout-Meeting-Notes-1501da993e978066bdcdc198cfec453b?pvs=21) 

[Remote Data CRM Column Drop Migration Plan](https://www.notion.so/Remote-Data-CRM-Column-Drop-Migration-Plan-18b1da993e9780d38ef4cd78a21bbe7d?pvs=21)