"""AMQP-only helpers. Spawned publishers never import Django or access the DB."""
import multiprocessing
import pika


def connect(config):
    return pika.BlockingConnection(pika.ConnectionParameters(host=config['host'],port=config['port'],virtual_host=config['virtual_host'],
        credentials=pika.PlainCredentials(config['user'],config['password']),connection_attempts=1,
        socket_timeout=3,stack_timeout=5,blocked_connection_timeout=3,heartbeat=10))


def close(connection):
    if connection is not None and connection.is_open:
        try:
            connection.close()
        except (pika.exceptions.AMQPError,OSError):
            pass


def publish_one(config,body,message_id):
    connection=None
    sending=False
    try:
        connection=connect(config)
        channel=connection.channel()
        channel.queue_declare(queue=config['queue'],durable=True)
        channel.confirm_delivery()
        sending=True
        channel.basic_publish(exchange='',routing_key=config['queue'],mandatory=True,body=body,
            properties=pika.BasicProperties(delivery_mode=2,content_type='application/json',message_id=message_id))
        return 'published'
    except pika.exceptions.UnroutableError:
        return 'unroutable'
    except pika.exceptions.NackError:
        return 'nacked'
    except (pika.exceptions.AMQPError,OSError):
        return 'uncertain' if sending else 'unavailable'
    finally:
        close(connection)


def _publish_to_pipe(config,body,message_id,writer):
    try:
        writer.send(publish_one(config,body,message_id))
    finally:
        writer.close()


def publish_message(config,body,message_id):
    # A live broker can omit confirms forever. The parent owns this process and
    # enforces a total deadline, without thread-unsafe/private Pika socket access.
    context=multiprocessing.get_context('spawn')
    reader,writer=context.Pipe(duplex=False)
    process=context.Process(target=_publish_to_pipe,args=(config,body,message_id,writer),daemon=True)
    started=False
    try:
        process.start();started=True;writer.close()
        if not reader.poll(10):
            return 'uncertain'
        try:
            return reader.recv()
        except EOFError:
            return 'uncertain'
    except OSError:
        return 'uncertain'
    finally:
        reader.close();writer.close()
        if started:
            process.join(timeout=.2)
            if process.is_alive():
                process.terminate();process.join(timeout=1)
            if process.is_alive():
                process.kill();process.join(timeout=1)
            process.close()
