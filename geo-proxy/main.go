// geo-proxy: per-port geo-routing HTTP/HTTPS proxy
// Each port routes all traffic through a specific country's proxy.
// No auth required — country is determined by the port the client connected to.
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

// GeoEntry maps a port to a country and its upstream proxy address.
type GeoEntry struct {
	Geo          string
	Name         string
	UpstreamAddr string
}

// PORT_MAP: listen port → geo country + upstream proxy
// Add/remove entries here to enable/disable countries.
var PORT_MAP = map[int]GeoEntry{
	9101: {"IN", "India",          "143.244.60.33:8099"},
	9102: {"GB", "United Kingdom", "131.153.1.42:8229"},
	9103: {"VN", "Vietnam",        "143.244.60.33:8237"},
	9104: {"MY", "Malaysia",       "143.244.60.33:8133"},
	9105: {"SG", "Singapore",      "79.127.248.200:8195"},
	9106: {"JP", "Japan",          "131.153.163.154:8110"},
	9107: {"HK", "Hong Kong",      "79.127.248.201:8096"},
	9108: {"CA", "Canada",         "23.235.247.82:8038"},
	9109: {"FR", "France",         "174.138.161.194:8072"},
	9110: {"PL", "Poland",         "79.127.248.200:8176"},
	9111: {"MX", "Mexico",         "174.138.162.218:8142"},
	9112: {"ZA", "South Africa",   "174.138.161.218:8200"},
	9113: {"BD", "Bangladesh",     "79.127.248.199:8018"},
	9114: {"PK", "Pakistan",       "79.127.248.199:8167"},
	9115: {"HU", "Hungary",        "79.127.248.199:8097"},
	9116: {"KZ", "Kazakhstan",     "79.127.248.200:8112"},
	9117: {"CL", "Chile",          "131.153.1.42:8043"},
	9118: {"KR", "South Korea",    "169.150.222.221:8116"},
	9121: {"US", "United States",  "23.111.180.230:8230"},
	9122: {"AE", "UAE",            "174.138.161.154:8228"},
	9123: {"AT", "Austria",        "131.153.163.234:8014"},
	9124: {"AU", "Australia",      "174.138.165.138:8013"},
	9125: {"CH", "Switzerland",    "131.153.163.146:8211"},
	9126: {"DE", "Germany",        "174.138.167.250:8080"},
	9127: {"ES", "Spain",          "174.138.162.242:8202"},
	9128: {"IE", "Ireland",        "174.138.162.138:8104"},
	9129: {"IT", "Italy",          "174.138.161.186:8106"},
	9130: {"KE", "Kenya",          "131.153.163.218:8113"},
	9131: {"NL", "Netherlands",    "174.138.161.202:8155"},
	9132: {"NZ", "New Zealand",    "131.153.163.26:8158"},
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

	// Direct mode (US): connect straight to target, no upstream proxy
	if entry.UpstreamAddr == "" {
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

	// Connect to the upstream geo proxy
	upstream, err := net.DialTimeout("tcp", entry.UpstreamAddr, 15*time.Second)
	if err != nil {
		log.Printf("[%s] upstream %s unreachable: %v", entry.Geo, entry.UpstreamAddr, err)
		conn.Write([]byte("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"))
		return
	}
	defer upstream.Close()

	upstream.SetDeadline(time.Now().Add(15 * time.Second))

	if req.Method == "CONNECT" {
		// HTTPS tunnel: ask upstream geo proxy to CONNECT to target
		// Retry once on failure (transient upstream issues)
		var upReader *bufio.Reader
		var resp *http.Response
		var connectErr error
		for attempt := 0; attempt < 2; attempt++ {
			if attempt > 0 {
				upstream.Close()
				upstream, connectErr = net.DialTimeout("tcp", entry.UpstreamAddr, 15*time.Second)
				if connectErr != nil {
					break
				}
				upstream.SetDeadline(time.Now().Add(15 * time.Second))
				defer upstream.Close()
			}
			fmt.Fprintf(upstream, "CONNECT %s HTTP/1.0\r\nHost: %s\r\n\r\n", req.Host, req.Host)
			upReader = bufio.NewReader(upstream)
			resp, connectErr = http.ReadResponse(upReader, req)
			if connectErr == nil && resp.StatusCode == 200 {
				break
			}
			log.Printf("[%s] upstream CONNECT to %s attempt %d failed (status=%v err=%v)", entry.Geo, req.Host, attempt+1, resp, connectErr)
		}
		if connectErr != nil || resp == nil || resp.StatusCode != 200 {
			conn.Write([]byte("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n"))
			return
		}

		// Tell client the tunnel is ready
		conn.Write([]byte("HTTP/1.1 200 Connection established\r\n\r\n"))

		// Bidirectional pipe until either side closes
		done := make(chan struct{}, 2)
		// Reset deadlines for the streaming phase
		conn.SetDeadline(time.Time{})
		upstream.SetDeadline(time.Time{})
		go pipe(upstream, conn, done)
		go pipe(conn, upstream, done)
		<-done

	} else {
		// Plain HTTP: forward full request to upstream as a proxy request
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
	log.Printf("[%s] %s listening on :%d → %s", entry.Geo, entry.Name, port, entry.UpstreamAddr)
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
