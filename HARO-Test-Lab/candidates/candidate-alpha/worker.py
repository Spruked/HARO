"""Fixed CPU fixture worker; does not run arbitrary shell commands."""
import hashlib, json, platform, sys, time

def main():
    job = json.load(sys.stdin)
    p = job['payload']
    if job['kind'] == 'fail':
        raise RuntimeError('Intentional failure fixture')
    if job['kind'] == 'sleep':
        time.sleep(min(30, max(0, float(p.get('seconds', 0.01)))))
        digest = 'slept'
    elif job['kind'] == 'hash':
        iterations = int(p.get('iterations', 20000))
        if not 1 <= iterations <= 1000000:
            raise ValueError('iterations must be between 1 and 1000000')
        data = str(p.get('text', 'fixture')).encode()
        if len(data) > 1000000:
            raise ValueError('Fixture payload too large')
        for _ in range(iterations):
            data = hashlib.sha256(data).digest()
        digest = data.hex()
    else:
        raise ValueError('Unsupported workload kind')
    peak = None
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak = int((rss/(1024*1024) if platform.system() == 'Darwin' else rss/1024)+1)
    except ImportError:
        pass
    print(json.dumps(dict(digest=digest, peak_rss_mib=peak)))

if __name__ == '__main__':
    main()
