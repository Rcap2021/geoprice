// geo-proxy: per-port geo-routing HTTP/HTTPS proxy
// Each port routes all traffic through a specific country's proxy.
// No auth required — country is determined by the port the client connected to.
// Multiple upstream addresses per geo: tried in order, failover on connect error.
package main

import (
	"bufio"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"time"
)

// GeoEntry maps a port to a country and its upstream proxy addresses.
// Addresses are tried in order; first successful connection wins.
type GeoEntry struct {
	Geo           string
	Name          string
	UpstreamAddrs []string // empty = direct (no upstream proxy)
}

// dialUpstream tries each address in order and returns the first that connects.
func dialUpstream(addrs []string, timeout time.Duration) (net.Conn, string, error) {
	var lastErr error
	for _, addr := range addrs {
		conn, err := net.DialTimeout("tcp", addr, timeout)
		if err == nil {
			return conn, addr, nil
		}
		log.Printf("upstream %s unreachable: %v — trying next", addr, err)
		lastErr = err
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("no upstream addresses configured")
	}
	return nil, "", lastErr
}

// PORT_MAP: listen port → geo country + upstream proxies (in priority order)
var PORT_MAP = map[int]GeoEntry{
	9101: {"IN", "India",         []string{"143.244.60.33:8099"}},
	9102: {"GB", "United Kingdom",[]string{"131.153.1.42:8229","131.153.162.74:8229","198.24.190.234:8229","131.153.162.66:8229"}},
	9103: {"VN", "Vietnam",       []string{"143.244.60.33:8237"}},
	9104: {"MY", "Malaysia",      []string{"143.244.60.33:8133"}},
	9105: {"SG", "Singapore",     []string{"79.127.248.200:8195"}},
	9106: {"JP", "Japan",         []string{"131.153.163.154:8110"}},
	9107: {"HK", "Hong Kong",     []string{"79.127.248.201:8096"}},
	9108: {"CA", "Canada",        []string{"23.235.247.82:8038","89.187.175.133:8038","152.233.22.58:8038","95.173.192.105:8038"}},
	9109: {"FR", "France",        []string{"174.138.161.194:8072"}},
	9110: {"PL", "Poland",        []string{"79.127.248.200:8176"}},
	9111: {"MX", "Mexico",        []string{"174.138.162.218:8142","174.138.162.234:8142","174.138.162.202:8142"}},
	9112: {"ZA", "South Africa",  []string{"174.138.161.218:8200","174.138.163.130:8200","84.17.40.181:8200"}},
	9113: {"BD", "Bangladesh",    []string{"79.127.248.199:8018"}},
	9114: {"PK", "Pakistan",      []string{"79.127.248.199:8167"}},
	9115: {"HU", "Hungary",       []string{"79.127.248.199:8097"}},
	9116: {"KZ", "Kazakhstan",    []string{"79.127.248.200:8112"}},
	9117: {"CL", "Chile",         []string{"131.153.1.42:8043","131.153.162.74:8043","198.24.190.234:8043","131.153.162.66:8043"}},
	9118: {"KR", "South Korea",   []string{"169.150.222.221:8116","95.173.204.33:8116","79.127.159.167:8116","79.127.248.199:8116"}},
	9121: {"US", "United States", []string{"23.111.180.230:8230","209.133.221.214:8230","66.165.244.6:8230","66.165.241.74:8230"}},
	9122: {"AE", "UAE",           []string{"174.138.161.154:8228"}},
	9123: {"AT", "Austria",       []string{"131.153.163.234:8014"}},
	9124: {"AU", "Australia",     []string{"174.138.165.138:8013","174.138.165.106:8013","174.138.165.122:8013"}},
	9125: {"CH", "Switzerland",   []string{"131.153.163.146:8211"}},
	9126: {"DE", "Germany",       []string{"174.138.167.250:8080","174.138.162.34:8080","192.154.254.56:8080","108.170.14.154:8080"}},
	9127: {"ES", "Spain",         []string{"174.138.162.242:8202","174.138.163.50:8202","174.138.163.130:8202","174.138.168.74:8202"}},
	9128: {"IE", "Ireland",       []string{"174.138.162.138:8104","174.138.165.82:8104","174.138.165.66:8104","174.138.165.74:8104"}},
	9129: {"IT", "Italy",         []string{"174.138.161.186:8106"}},
	9130: {"KE", "Kenya",         []string{"131.153.163.218:8113"}},
	9131: {"NL", "Netherlands",   []string{"174.138.161.202:8155"}},
	9132: {"NZ", "New Zealand",   []string{"131.153.163.26:8158"}},
}

func pipe(dst net.Conn, src net.Conn, done chan struct{}) {
	defer func() {
		select {
		case done <- struct{}{}:
		default:
		}
	}()
	io.Copy(dst, src)
}

func handleConn(conn net.Conn, entry GeoEntry) {
	defer conn.Close()

	log.Printf("[%s] connection from %s", entry.Geo, conn.RemoteAddr())

	conn.SetDeadline(time.Now().Add(90 * time.Second))

	reader := bufio.NewReader(conn)
	req, err := http.ReadRequest(reader)
	if err != nil {
		log.Printf("[%s] read request error from %s: %v", entry.Geo, conn.RemoteAddr(), err)
		return
	}
	log.Printf("[%s] %s %s from %s", entry.Geo, req.Method, req.Host, conn.RemoteAddr())

	// Direct mode: no upstream addrs — connect straight to target
	if len(entry.UpstreamAddrs) == 0 {
		if req.Method == "CONNECT" {
			target, err := net.DialTimeout("tcp", req.Host, 15*time.Second)
			if err != nil {
				log.Printf("[%s] direct CONNECT to %s failed: %v", entry.Geo, req.Host, err)
				conn.Write([]byte("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"))
				return
			}
			defer target.Close()
			conn.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))
			conn.SetDeadline(time.Time{})
			target.SetDeadline(time.Time{})
			done := make(chan struct{}, 2)
			go pipe(target, conn, done)
			go pipe(conn, target, done)
			<-done
		} else {
			target, err := net.DialTimeout("tcp", req.Host+":80", 15*time.Second)
			if err != nil {
				return
			}
			defer target.Close()
			req.WriteProxy(target)
			conn.SetDeadline(time.Time{})
			target.SetDeadline(time.Time{})
			io.Copy(conn, target)
		}
		return
	}

	// Connect to upstream — try all addresses in order
	upstream, usedAddr, err := dialUpstream(entry.UpstreamAddrs, 15*time.Second)
	if err != nil {
		log.Printf("[%s] all upstreams failed for %s: %v", entry.Geo, req.Host, err)
		conn.Write([]byte("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"))
		return
	}
	defer upstream.Close()
	log.Printf("[%s] using upstream %s", entry.Geo, usedAddr)

	upstream.SetDeadline(time.Now().Add(15 * time.Second))

	if req.Method == "CONNECT" {
		// HTTPS tunnel: forward CONNECT to upstream, retry on next peer on failure
		var upReader *bufio.Reader
		var resp *http.Response
		var connectErr error

		fmt.Fprintf(upstream, "CONNECT %s HTTP/1.0\r\nHost: %s\r\n\r\n", req.Host, req.Host)
		upReader = bufio.NewReader(upstream)
		resp, connectErr = http.ReadResponse(upReader, req)

		if connectErr != nil || resp == nil || resp.StatusCode != 200 {
			log.Printf("[%s] CONNECT to %s via %s failed (status=%v err=%v) — trying next peer", entry.Geo, req.Host, usedAddr, resp, connectErr)
			upstream.Close()

			// Try remaining peers
			remaining := []string{}
			for _, a := range entry.UpstreamAddrs {
				if a != usedAddr {
					remaining = append(remaining, a)
				}
			}
			upstream2, addr2, err2 := dialUpstream(remaining, 15*time.Second)
			if err2 != nil {
				conn.Write([]byte("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"))
				return
			}
			defer upstream2.Close()
			upstream2.SetDeadline(time.Now().Add(15 * time.Second))
			fmt.Fprintf(upstream2, "CONNECT %s HTTP/1.0\r\nHost: %s\r\n\r\n", req.Host, req.Host)
			upReader = bufio.NewReader(upstream2)
			resp, connectErr = http.ReadResponse(upReader, req)
			if connectErr != nil || resp == nil || resp.StatusCode != 200 {
				conn.Write([]byte("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"))
				return
			}
			log.Printf("[%s] CONNECT to %s succeeded via fallback %s", entry.Geo, req.Host, addr2)
			upstream = upstream2
		}

		conn.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))
		done := make(chan struct{}, 2)
		conn.SetDeadline(time.Time{})
		upstream.SetDeadline(time.Time{})
		go pipe(upstream, conn, done)
		go pipe(conn, upstream, done)
		<-done

	} else {
		// Plain HTTP: forward to upstream
		if err := req.WriteProxy(upstream); err != nil {
			return
		}
		conn.SetDeadline(time.Time{})
		upstream.SetDeadline(time.Time{})
		io.Copy(conn, upstream)
	}
}

func listenGeo(port int, entry GeoEntry) {
	ln, err := net.Listen("tcp", fmt.Sprintf("0.0.0.0:%d", port))
	if err != nil {
		log.Fatalf("[%s] failed to bind port %d: %v", entry.Geo, port, err)
	}
	addrs := entry.UpstreamAddrs
	if len(addrs) == 0 {
		addrs = []string{"direct"}
	}
	log.Printf("[%s] %s listening on :%d → %v (%d peers)", entry.Geo, entry.Name, port, addrs[0], len(addrs))
	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Printf("[%s] accept error: %v", entry.Geo, err)
			time.Sleep(100 * time.Millisecond)
			continue
		}
		go handleConn(conn, entry)
	}
}

func main() {
	log.SetFlags(log.LstdFlags)
	log.Printf("GeoProxy starting — %d countries", len(PORT_MAP))

	for port, entry := range PORT_MAP {
		go listenGeo(port, entry)
	}

	// Block forever
	select {}
}
